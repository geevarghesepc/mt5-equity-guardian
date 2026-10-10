"""Event-aware two-position stop policy.

Replaces the old combined-risk / global-loser behaviour with:
  1. Baseline awareness — positions already open when the guardian starts
     are subject to the global cap but not to the pyramiding profit test.
  2. Same-symbol admission — when a new managed ticket appears on a symbol
     that already has a managed ticket, the older one is closed if its
     floating profit is <= 0.  Kept if profit > 0.  The decision is made
     once, at the moment the new ticket is first detected.
  3. Global cap of two managed positions — after same-symbol handling,
     close the oldest by ``(time, ticket)`` until at most two remain.
  4. Failed closes are retained for retry on the next iteration.
"""

import logging

logger = logging.getLogger(__name__)

MAX_OPEN_POSITIONS = 2


class PositionPolicy:
    """Stateful position admission tracker.

    Attributes
    ----------
    baseline_tickets : set[int]
        Tickets that were open when the guardian first observed positions.
        These are exempt from the same-symbol profit test.
    known_tickets : set[int]
        All ticket IDs the policy has ever seen.
    decided_tickets : dict[int, str]
        Tickets for which a same-symbol decision was already made.
        Values: ``"keep"`` or ``"close"``.
    pending_closes : dict[int, dict]
        Tickets that should be closed but the close failed last time.
        Maps ticket → position dict for retry.
    _baseline_set : bool
        Whether the first observation has been recorded.
    """

    def __init__(self, max_open=MAX_OPEN_POSITIONS):
        self.max_open = max_open
        self.baseline_tickets: set[int] = set()
        self.known_tickets: set[int] = set()
        self.decided_tickets: dict[int, str] = {}
        self.decided_pairs: dict[tuple[int, int], str] = {}
        self.pending_closes: dict[int, dict] = {}
        self._baseline_set = False

    def set_baseline(self, positions: list[dict]):
        """Record the positions present at guardian startup.

        Called exactly once.  Positions here are subject to the global cap
        but *not* to the same-symbol profit test when a new ticket arrives.
        """
        if self._baseline_set:
            return
        self.baseline_tickets = {int(p["ticket"]) for p in positions}
        self.known_tickets = set(self.baseline_tickets)
        self._baseline_set = True
        logger.info(
            "Baseline set with %d position(s): %s",
            len(self.baseline_tickets),
            sorted(self.baseline_tickets),
        )

    def reset(self):
        """Reset all policy state (used on account switch or restart)."""
        self.baseline_tickets.clear()
        self.known_tickets.clear()
        self.decided_tickets.clear()
        self.decided_pairs.clear()
        self.pending_closes.clear()
        self._baseline_set = False
        logger.info("PositionPolicy state reset.")

    @property
    def baseline_ready(self) -> bool:
        return self._baseline_set

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def evaluate(self, positions: list[dict], config: dict) -> list[dict]:
        """Return a list of position dicts that should be closed.

        The caller must attempt to close each one and report back via
        :meth:`report_close_result`.

        Order of returned list:
          1. Same-symbol decisions (close the prior ticket of the same symbol).
          2. Global cap evictions (oldest first by ``(time, ticket)``).
          3. Outstanding retries from earlier failed closes.
        """
        if not positions:
            # Purge pending closes — positions are gone.
            self.pending_closes.clear()
            return []

        if not self._baseline_set:
            self.set_baseline(positions)

        max_open = int(
            config.get("anti_averaging", {}).get(
                "max_open_positions", self.max_open
            )
        )

        current_tickets = {int(p["ticket"]) for p in positions}

        # Detect new tickets (ones we have never seen).
        new_tickets = current_tickets - self.known_tickets
        self.known_tickets |= current_tickets

        # Build a by-ticket lookup and a by-symbol grouping.
        by_ticket: dict[int, dict] = {int(p["ticket"]): p for p in positions}
        by_symbol: dict[str, list[dict]] = {}
        for p in positions:
            by_symbol.setdefault(p["symbol"], []).append(p)

        # Purge pending closes for tickets that are no longer open,
        # and refresh pending snapshots with the latest live position data.
        for t in list(self.pending_closes):
            if t not in current_tickets:
                del self.pending_closes[t]
            else:
                self.pending_closes[t] = by_ticket[t]

        pending_ticket_set = set(self.pending_closes.keys())
        to_close: list[dict] = []  # ordered list of positions to close
        closed_tickets: set[int] = set()  # tickets already slated for close in this pass

        # ---- Step 1: Same-symbol admission for each new ticket ----------
        for ticket in sorted(new_tickets):
            pos = by_ticket.get(ticket)
            if pos is None:
                continue
            symbol = pos["symbol"]
            same_symbol = sorted(
                by_symbol.get(symbol, []),
                key=lambda x: (x["time"], x["ticket"]),
            )
            # Find the immediately previous ticket of the same symbol not already slated to close.
            prev = None
            for sp in same_symbol:
                sp_ticket = int(sp["ticket"])
                if sp_ticket == ticket:
                    break
                if sp_ticket not in closed_tickets and sp_ticket not in pending_ticket_set:
                    prev = sp

            if prev is None:
                continue

            prev_ticket = int(prev["ticket"])
            pair = (prev_ticket, ticket)
            if pair in self.decided_pairs:
                continue

            profit = float(prev.get("profit", 0.0))
            if profit <= 0:
                self.decided_pairs[pair] = "close"
                self.decided_tickets[prev_ticket] = "close"
                if prev_ticket not in closed_tickets:
                    to_close.append(prev)
                    closed_tickets.add(prev_ticket)
                    logger.info(
                        "Same-symbol close: ticket %d (%s) profit=%.2f <= 0 for new ticket %d.",
                        prev_ticket,
                        prev["symbol"],
                        profit,
                        ticket,
                    )
            else:
                self.decided_pairs[pair] = "keep"
                logger.info(
                    "Same-symbol keep: ticket %d (%s) profit=%.2f > 0 for new ticket %d.",
                    prev_ticket,
                    prev["symbol"],
                    profit,
                    ticket,
                )

        # ---- Step 2: Global cap — oldest first by (time, ticket) --------
        # Positions that are already scheduled to be closed (either from this loop's
        # same-symbol admission or from existing pending closes) will not survive.
        # Enforce the max_open cap only on the positions that will remain open.
        scheduled_for_close = closed_tickets | pending_ticket_set
        remaining = [
            p
            for p in positions
            if int(p["ticket"]) not in scheduled_for_close
        ]
        remaining.sort(key=lambda x: (x["time"], x["ticket"]))

        while len(remaining) > max_open:
            oldest = remaining.pop(0)
            oldest_ticket = int(oldest["ticket"])
            if oldest_ticket not in closed_tickets:
                to_close.append(oldest)
                closed_tickets.add(oldest_ticket)
                scheduled_for_close.add(oldest_ticket)
                logger.info(
                    "Global cap eviction: ticket %d (%s), "
                    "remaining would be %d > max %d.",
                    oldest_ticket,
                    oldest["symbol"],
                    len(remaining) + 1,
                    max_open,
                )

        # ---- Step 3: Outstanding retries --------------------------------
        for ticket, pos in list(self.pending_closes.items()):
            if ticket in current_tickets and ticket not in closed_tickets:
                to_close.append(pos)
                closed_tickets.add(ticket)
                logger.debug(
                    "Retry pending close: ticket %d (%s).",
                    ticket,
                    pos["symbol"],
                )

        return to_close

    def report_close_result(self, ticket: int, position: dict, success: bool):
        """Record whether a close attempt succeeded or failed.

        Failed closes are retained in ``pending_closes`` for the next
        iteration.
        """
        ticket = int(ticket)
        if success:
            self.pending_closes.pop(ticket, None)
        else:
            if ticket not in self.pending_closes:
                logger.warning(
                    "Close failed for ticket %d; queued for retry.", ticket
                )
            else:
                logger.debug(
                    "Close still pending for ticket %d.", ticket
                )
            self.pending_closes[ticket] = position


# ---------------------------------------------------------------------------
# Legacy compat — thin wrapper that constructs an ephemeral policy.
# The guardian loop should use PositionPolicy directly.
# ---------------------------------------------------------------------------

def evaluate(positions, config):
    """Stateless fallback.  Prefer :class:`PositionPolicy` for proper
    baseline and retry tracking."""
    policy = PositionPolicy()
    return policy.evaluate(positions, config)
