"""Business-logic services (DOC section 24).

Each service owns one domain and is the ONLY place that may write to the
corresponding collection. Routers call services; services call repositories or
the database directly. One service per planned module:

    auth          registration, login, password reset, token lifecycle
    vehicle       vehicle CRUD and default-vehicle selection
    facility      facility + floor/zone/space management
    availability  live and simulated space-state transitions
    reservation   reservation rules, concurrency, grace periods, refunds
    session       check-in / check-out / extend, QR validation
    pricing       rate calculation, peak & dynamic pricing, rate previews
    payment       provider abstraction, receipts, refunds
    recommendation  weighted ranking (DOC section 15)
    prediction    historical occupancy forecast
    analytics     KPIs and reports
    notification  in-app + email delivery
    violation     violation recording and resolution
    complaint     support case workflow
    ev            chargers and charging sessions
    event         event / temporary parking demand
    audit         critical-action trail
    waitlist      queue for full facilities
"""
