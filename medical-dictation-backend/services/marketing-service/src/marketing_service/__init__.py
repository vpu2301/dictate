"""marketing-service — the public demo-booking funnel.

Two transactional emails, and nothing else:

  1. Someone submits "Book a demo" on klarnote.com. They get an
     acknowledgement carrying the button that opens the Google
     appointment page.
  2. They pick a slot there. Google mails the invitation to the sales
     mailbox; this service watches that mailbox, matches the attendee
     back to the request, and sends the branded confirmation with the
     real date, time and Meet link.

Deliberately separate from notification-service. That service is
tenant-scoped, authenticated and sits on PHI; this one is
unauthenticated, public-facing, and holds nothing but work email
addresses and organisation names. Keeping the public attack surface out
of the PHI service is the whole reason it is its own process.
"""
