"""Background jobs (DOC section 24).

Phase 1 reserves the directory; the scheduler itself lands with the phases
that need it. Planned jobs:

    reservation_expiry   release capacity past endTime          (Phase 3)
    no_show_sweep        mark CONFIRMED past grace period NO_SHOW (Phase 3)
    reservation_reminder "starts in 30 minutes" notifications   (Phase 5)
    overstay_monitor     detect sessions past their window      (Phase 4)
    occupancy_snapshot   append occupancyLogs for analytics    (Phase 7)
    prediction_refresh   recompute predictionResults           (Phase 7)
    waitlist_notify      alert queued drivers on capacity       (Phase 8)
"""
