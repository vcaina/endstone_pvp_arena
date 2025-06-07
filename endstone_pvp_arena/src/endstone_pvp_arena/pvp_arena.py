import uuid

from endstone.event import (
    ActorDeathEvent,
    PlayerDeathEvent,
    PlayerQuitEvent,
    PlayerJoinEvent,
    event_handler,
)
from endstone.form import ActionForm
from endstone.level import Location
from endstone.plugin import Plugin
from endstone.scoreboard import Criteria
from endstone.boss import BarColor, BarStyle, BossBar


class PvPArena(Plugin):
    api_version = "0.5"
    name = "pvp_arena"

    commands = {
        "pvp": {
            "description": "Open the PvP menu.",
            "usages": ["/pvp"],
            "permissions": ["pvp_arena.command.pvp"],
        },
        "forceend": {
            "description": "Force finish an active duel (testing).",
            "usages": ["/forceend"],
            "permissions": ["pvp_arena.command.forceend"],
        },
    }

    permissions = {
        "pvp_arena.command.pvp": {
            "description": "Allow using /pvp to open the duel UI.",
            "default": True,
        },
        "pvp_arena.command.forceend": {
            "description": "Allow using /forceend for debugging.",
            "default": True,
        },
    }

    ARENA_DIMENSION = "overworld"
    ARENA_X = 0
    ARENA_Y = 100
    ARENA_Z = 0

    def __init__(self) -> None:
        super().__init__()
        # Use player names for pending requests
        self._pending: dict[str, list[str]] = {}
        # Track duel state by player UUID so entries persist across deaths
        self._duels: dict[uuid.UUID, uuid.UUID] = {}
        self._inventories: dict[uuid.UUID, list] = {}
        self._locations: dict[uuid.UUID, Location] = {}
        self._bars: dict[uuid.UUID, BossBar] = {}

    def _set_keep_inventory(self, value: bool) -> None:
        """Toggle the keepinventory gamerule via console command."""
        try:
            state = "true" if value else "false"
            self.server.dispatch_command(
                self.server.command_sender, f"gamerule keepinventory {state}"
            )
        except Exception as exc:
            self.logger.error(f"Failed to set keepinventory: {exc}")

    def on_enable(self) -> None:
        sb = self.server.scoreboard
        if not sb.get_objective("pvp_wins"):
            sb.add_objective("pvp_wins", Criteria.Type.DUMMY, "PvP Wins")
        if not sb.get_objective("elo_rating"):
            sb.add_objective("elo_rating", Criteria.Type.DUMMY, "ELO Rating")
        self.register_events(self)
        self.logger.info("PvP Arena events registered")

    # Menu helpers
    def _show_main_menu(self, player) -> None:
        form = ActionForm("PvP Menu")
        form.add_button("Challenge Player")
        form.add_button("Pending Requests")
        form.add_button("Leaderboards")

        def handle(p, index):
            if index == 0:
                self._show_player_list(p)
            elif index == 1:
                self._show_pending(p)
            else:
                self._show_leaderboards(p)

        form.on_submit = handle
        player.send_form(form)

    def _show_player_list(self, player) -> None:
        others = [p for p in self.server.online_players if p != player]
        form = ActionForm("Select Opponent")
        if not others:
            form.content = "No players online"
        for o in others:
            elo = self._get_elo(o)
            form.add_button(f"{o.name} ({elo})")

        def handle(p, index):
            if 0 <= index < len(others):
                target = others[index]
                self._pending.setdefault(target.name, []).append(p.name)
                p.send_tip(f"Duel request sent to {target.name}")
                target.send_tip(f"{p.name} challenged you. Use /pvp to respond.")

        form.on_submit = handle
        player.send_form(form)

    def _show_pending(self, player) -> None:
        requests = [self.server.get_player(name) for name in self._pending.get(player.name, [])]
        requests = [r for r in requests if r]
        form = ActionForm("Pending Requests")
        if not requests:
            form.content = "No pending requests"
        for r in requests:
            form.add_button(r.name)

        def handle(p, index):
            if 0 <= index < len(requests):
                challenger = requests[index]
                self._pending[player.name].remove(challenger.name)
                self._start_duel(challenger, p)

        form.on_submit = handle
        player.send_form(form)

    def _show_leaderboards(self, player) -> None:
        form = ActionForm("Leaderboards")
        form.add_button("ELO Rating")
        form.add_button("Win Count")

        def handle(p, index):
            if index == 0:
                self._show_elo_leaderboard(p)
            elif index == 1:
                self._show_wins_leaderboard(p)

        form.on_submit = handle
        player.send_form(form)

    def _show_wins_leaderboard(self, player) -> None:
        obj = self.server.scoreboard.get_objective("pvp_wins")
        scores = []
        for entry in obj.scoreboard.entries:
            score = obj.get_score(entry)
            if score.is_score_set:
                name = entry.name if hasattr(entry, "name") else str(entry)
                scores.append((name, score.value))
        scores.sort(key=lambda t: t[1], reverse=True)
        form = ActionForm("Win Leaderboard")
        content = "\n".join(f"{name}: {score}" for name, score in scores[:10]) or "No scores"
        form.content = content
        player.send_form(form)

    def _show_elo_leaderboard(self, player) -> None:
        obj = self.server.scoreboard.get_objective("elo_rating")
        scores = []
        for entry in obj.scoreboard.entries:
            score = obj.get_score(entry)
            if score.is_score_set:
                name = entry.name if hasattr(entry, "name") else str(entry)
                scores.append((name, score.value))
        scores.sort(key=lambda t: t[1], reverse=True)
        form = ActionForm("ELO Leaderboard")
        content = "\n".join(f"{name}: {score}" for name, score in scores[:10]) or "No scores"
        form.content = content
        player.send_form(form)

    def _arena_location(self) -> Location:
        level = self.server.level
        dim = level.get_dimension(self.ARENA_DIMENSION)
        return Location(dim, self.ARENA_X, self.ARENA_Y, self.ARENA_Z)

    def _get_elo(self, entry) -> int:
        obj = self.server.scoreboard.get_objective("elo_rating")
        score = obj.get_score(entry)
        if not score.is_score_set:
            score.value = 1000
        return score.value

    def _update_elo(self, winner, loser) -> None:
        obj = self.server.scoreboard.get_objective("elo_rating")
        w_score = obj.get_score(winner)
        l_score = obj.get_score(loser)
        if not w_score.is_score_set:
            w_score.value = 1000
        if not l_score.is_score_set:
            l_score.value = 1000
        w_old = w_score.value
        l_old = l_score.value
        expected_w = 1 / (1 + 10 ** ((l_old - w_old) / 400))
        expected_l = 1 - expected_w
        k = 32
        w_new = round(w_old + k * (1 - expected_w))
        l_new = round(l_old + k * (0 - expected_l))
        w_score.value = int(w_new)
        l_score.value = int(l_new)

    def _restore_inventory(self, player) -> None:
        inv = self._inventories.get(player.unique_id)
        if inv is not None:
            player.inventory.clear()
            for idx, item in enumerate(inv):
                if item:
                    player.inventory.set_item(idx, item)

    def _update_bar(self, p1, p2) -> None:
        """Create or update the boss bar showing the duel state."""
        title = f"{p1.name} vs {p2.name}"
        bar = self._bars.get(p1.unique_id)
        if not bar:
            bar = self.server.create_boss_bar(title, BarColor.RED, BarStyle.SOLID)
            bar.add_player(p1)
            bar.add_player(p2)
            bar.is_visible = True
            self._bars[p1.unique_id] = bar
            self._bars[p2.unique_id] = bar
        else:
            bar.title = title
            bar.is_visible = True

    def _reset_round(self, p1, p2) -> None:
        """Teleport duelists to the arena and display the duel banner."""
        self.logger.debug(
            f"Starting duel between {p1.name} and {p2.name}"
        )
        loc = self._arena_location()
        for p in (p1, p2):
            self._restore_inventory(p)
            p.health = p.max_health
            p.teleport(loc)

        vs = f"{p1.name} vs {p2.name}"
        p1.send_title(vs, "Fight!")
        p2.send_title(vs, "Fight!")
        self._update_bar(p1, p2)

    def _start_duel(self, p1, p2) -> None:
        self.logger.debug(f"Starting duel between {p1.name} and {p2.name}")
        for p in (p1, p2):
            self._inventories[p.unique_id] = list(p.inventory.contents)
            self._locations[p.unique_id] = p.location
        self._duels[p1.unique_id] = p2.unique_id
        self._duels[p2.unique_id] = p1.unique_id
        if len(self._duels) == 2:
            self._set_keep_inventory(True)
        self._reset_round(p1, p2)

    def _end_duel(self, winner, loser) -> None:
        if not winner or not loser:
            self.logger.info("Cannot end duel: missing player instance")
            return

        self.logger.debug(f"_end_duel called with winner={winner.name} loser={loser.name}")
        self.logger.info(f"Ending duel: {winner.name} defeated {loser.name}")

        obj = self.server.scoreboard.get_objective("pvp_wins")
        if hasattr(obj, "ensure_has_entry"):
            obj.ensure_has_entry(winner)
            obj.ensure_has_entry(loser)
        score = obj.get_score(winner)
        score.value = score.value + 1
        self.logger.info("Updated win count")
        self.logger.debug(
            f"Winner score now {score.value} for player {winner.name}"
        )

        self._update_elo(winner, loser)
        self.logger.info("Updated ELO scores")
        w_elo = self._get_elo(winner)
        l_elo = self._get_elo(loser)
        self.logger.debug(
            f"Winner elo={w_elo} loser elo={l_elo} after duel"
        )

        bar = self._bars.pop(winner.unique_id, None)
        if not bar:
            bar = self._bars.pop(loser.unique_id, None)
        else:
            self._bars.pop(loser.unique_id, None)
        if bar:
            bar.remove_player(winner)
            bar.remove_player(loser)
            self.logger.info("Removed players from boss bar")
            bar.is_visible = False
            bar.remove_all()
            self.logger.info("Cleared boss bar")
        else:
            self.logger.info("No boss bar to clear")

        for p in (winner, loser):
            self._restore_inventory(p)
            self._inventories.pop(p.unique_id, None)
            loc = self._locations.pop(p.unique_id, None)

            def finish_restore(player=p, location=loc) -> None:
                if location:
                    player.teleport(location)
                    self.logger.info(
                        f"Teleported {player.name} back to saved location"
                    )
                player.health = player.max_health

            self.server.scheduler.run_task(self, finish_restore, delay=40)
            self._duels.pop(p.unique_id, None)

        winner.send_title("Duel Won", "")
        loser.send_title("Duel Lost", "")
        self.server.broadcast_message(
            f"{winner.name} defeated {loser.name} in a duel!"
        )
        self.logger.info("Duel ended and announcement broadcasted")

        if not self._duels:
            self._set_keep_inventory(False)

    def _end_duel_disconnect(self, winner, loser_offline) -> None:
        """End a duel when the loser disconnected."""
        loser_id = loser_offline.unique_id
        loser_name = loser_offline.name

        obj = self.server.scoreboard.get_objective("pvp_wins")
        score = obj.get_score(winner)
        score.value = score.value + 1

        self._update_elo(winner, loser_name)

        bar = self._bars.pop(winner.unique_id, None)
        if not bar:
            bar = self._bars.pop(loser_id, None)
        if bar:
            bar.remove_player(winner)
            bar.is_visible = False
            bar.remove_all()

        self._restore_inventory(winner)
        self._inventories.pop(winner.unique_id, None)
        loc = self._locations.pop(winner.unique_id, None)
        if loc:
            winner.teleport(loc)
        winner.health = winner.max_health

        self._duels.pop(winner.unique_id, None)
        self._duels.pop(loser_id, None)

        self.server.broadcast_message(
            f"{winner.name} defeated {loser_name} in a duel (opponent disconnected)!"
        )
        self.logger.info("Duel ended due to disconnect")

        if not self._duels:
            self._set_keep_inventory(False)

    def _retry_end_duel(self, win_id: uuid.UUID, los_id: uuid.UUID, remaining: int) -> None:
        """Try to end the duel, retrying if players aren't available."""
        self.logger.debug(
            f"_retry_end_duel called win_id={win_id} los_id={los_id} remaining={remaining}"
        )

        def attempt() -> None:
            winner = self.server.get_player(win_id)
            loser = self.server.get_player(los_id)
            self.logger.debug(
                f"_retry_end_duel attempt winner={winner} loser={loser}"
            )
            if winner and loser:
                try:
                    self.logger.debug("Both players online, calling _end_duel")
                    self._end_duel(winner, loser)
                except Exception as exc:
                    self.logger.error(f"Failed to end duel: {exc}")
                    if remaining > 0:
                        self.logger.debug(
                            f"Retrying duel end in 2 ticks ({remaining} left)"
                        )
                        self.server.scheduler.run_task(
                            self,
                            lambda: self._retry_end_duel(win_id, los_id, remaining - 1),
                            delay=2,
                        )
            else:
                self.logger.warning(
                    f"Cannot end duel: players offline (winner={win_id}, loser={los_id})"
                )
                if remaining > 0:
                    self.logger.debug(
                        f"Players offline, retrying in 2 ticks ({remaining} left)"
                    )
                    self.server.scheduler.run_task(
                        self,
                        lambda: self._retry_end_duel(win_id, los_id, remaining - 1),
                        delay=2,
                    )

        attempt()

    # Event handlers
    @staticmethod
    @event_handler
    def on_player_death(event: PlayerDeathEvent) -> None:
        """Handle a player's death during a duel."""
        server = event.player.server
        plugin = server.plugin_manager.get_plugin("pvp_arena")
        if not isinstance(plugin, PvPArena):
            return
        server.logger.info(f"PlayerDeathEvent captured for {event.player.name}")
        plugin._handle_player_death(event)

    @staticmethod
    @event_handler
    def on_actor_death(event: ActorDeathEvent) -> None:
        """Fallback handler in case PlayerDeathEvent is not fired."""
        if not getattr(event.actor, "is_player", False):
            return
        server = event.actor.server
        plugin = server.plugin_manager.get_plugin("pvp_arena")
        if not isinstance(plugin, PvPArena):
            return
        server.logger.info(f"ActorDeathEvent captured for {event.actor.name}")
        if isinstance(event, PlayerDeathEvent):
            plugin._handle_player_death(event)
        else:
            # PlayerDeathEvent did not fire; handle using actor data
            plugin.logger.warning(
                f"Player death fallback triggered for {event.actor.name}"
            )
            plugin._handle_actor_death(event)

    @staticmethod
    @event_handler
    def on_player_quit(event: PlayerQuitEvent) -> None:
        server = event.player.server
        plugin = server.plugin_manager.get_plugin("pvp_arena")
        if isinstance(plugin, PvPArena):
            plugin._handle_player_quit(event)

    @staticmethod
    @event_handler
    def on_player_join(event: PlayerJoinEvent) -> None:
        server = event.player.server
        plugin = server.plugin_manager.get_plugin("pvp_arena")
        if isinstance(plugin, PvPArena):
            plugin._handle_player_join(event.player)

    def _handle_player_death(self, event: PlayerDeathEvent) -> None:
        self.logger.debug("_handle_player_death fired")
        killer = event.damage_source.actor
        victim = event.player
        self.logger.info(
            f"Death event: {victim.name} killed by {getattr(killer, 'name', 'None')}"
        )
        self.logger.debug(f"Current duels mapping: {self._duels}")

        killer_id = killer.unique_id if killer and getattr(killer, "is_player", False) else None
        victim_id = victim.unique_id

        if killer_id is not None:
            opponent_id = self._duels.get(killer_id)
            self.logger.debug(
                f"Lookup duel: killer={killer_id} victim={victim_id} opponent={opponent_id}"
            )
            if opponent_id == victim_id:
                self.logger.debug(
                    f"Duel over: {killer.name} defeated {victim.name}"
                )
                self.server.scheduler.run_task(
                    self,
                    lambda: self._retry_end_duel(killer_id, victim_id, 5),
                    delay=2,
                )
            else:
                self.logger.warning(
                    f"Duel state mismatch for kill: killer={killer_id} victim={victim_id} opponent={opponent_id}"
                )
        else:
            # No killer (environmental death). Attempt to resolve duel if victim is in one
            opponent_id = self._duels.get(victim_id)
            self.logger.debug(
                f"No killer found. victim_id={victim_id} opponent={opponent_id}"
            )
            if opponent_id:
                opponent = self.server.get_player(opponent_id)
                if opponent:
                    self.logger.warning(
                        f"Ending duel with fallback winner {opponent.name}"
                    )
                    self.server.scheduler.run_task(
                        self,
                        lambda: self._retry_end_duel(opponent_id, victim_id, 5),
                        delay=2,
                    )
                else:
                    self.logger.warning(
                        f"Opponent {opponent_id} offline during fallback duel end"
                    )

    def _handle_actor_death(self, event: ActorDeathEvent) -> None:
        """Fallback processing when only ActorDeathEvent is available."""
        self.logger.debug("_handle_actor_death fired")
        killer = event.damage_source.actor if event.damage_source else None
        victim = event.actor
        if not getattr(victim, "is_player", False):
            return
        self.logger.info(
            f"Fallback death event: {victim.name} killed by {getattr(killer, 'name', 'None')}"
        )
        self.logger.debug(f"Current duels mapping: {self._duels}")

        killer_id = killer.unique_id if killer and getattr(killer, "is_player", False) else None
        victim_id = victim.unique_id

        if killer_id is not None:
            opponent_id = self._duels.get(killer_id)
            self.logger.debug(
                f"Lookup duel: killer={killer_id} victim={victim_id} opponent={opponent_id}"
            )
            if opponent_id == victim_id:
                self.logger.debug(
                    f"Duel over: {killer.name} defeated {victim.name} (fallback)"
                )
                self.server.scheduler.run_task(
                    self,
                    lambda: self._retry_end_duel(killer_id, victim_id, 5),
                    delay=2,
                )
            else:
                self.logger.warning(
                    f"Duel state mismatch for kill: killer={killer_id} victim={victim_id} opponent={opponent_id}"
                )
        else:
            opponent_id = self._duels.get(victim_id)
            self.logger.debug(
                f"No killer found in actor event. victim_id={victim_id} opponent={opponent_id}"
            )
            if opponent_id:
                opponent = self.server.get_player(opponent_id)
                if opponent:
                    self.logger.warning(
                        f"Ending duel with fallback winner {opponent.name} (actor event)"
                    )
                    self.server.scheduler.run_task(
                        self,
                        lambda: self._retry_end_duel(opponent_id, victim_id, 5),
                        delay=2,
                    )
                else:
                    self.logger.warning(
                        f"Opponent {opponent_id} offline during actor fallback duel end"
                    )

    def _handle_player_quit(self, event: PlayerQuitEvent) -> None:
        """Handle a player leaving during a duel."""
        player = event.player
        leaver_id = player.unique_id
        opp_id = self._duels.pop(leaver_id, None)
        self._bars.pop(leaver_id, None)
        if not opp_id:
            return
        self._duels.pop(opp_id, None)
        opponent = self.server.get_player(opp_id)
        if opponent:
            self.logger.info(
                f"{player.name} disconnected, awarding duel to {opponent.name}"
            )
            self._end_duel_disconnect(opponent, player)
        else:
            self.logger.info(
                f"Both duelists offline after {player.name} quit; cleaning up"
            )

    def _handle_player_join(self, player) -> None:
        """Restore inventory for players who disconnected mid-duel."""
        if player.unique_id in self._inventories:
            self._restore_inventory(player)
            loc = self._locations.pop(player.unique_id, None)
            if loc:
                player.teleport(loc)
            self._inventories.pop(player.unique_id, None)
            player.health = player.max_health
            self.logger.info(f"Restored inventory for {player.name} on join")




    def on_command(self, sender, command, args):
        if not getattr(sender, "is_player", True):
            return False

        if command.name == "pvp":
            self._show_main_menu(sender)
            return True

        if command.name == "forceend":
            opp_id = self._duels.get(sender.unique_id)
            if not opp_id:
                sender.send_tip("You are not in a duel")
                return True
            opponent = self.server.get_player(opp_id)
            if not opponent:
                sender.send_tip("Opponent not online")
                return True
            try:
                self._end_duel(sender, opponent)
            except Exception as exc:
                self.logger.error(f"Forceend failed: {exc}")
            return True

        return False