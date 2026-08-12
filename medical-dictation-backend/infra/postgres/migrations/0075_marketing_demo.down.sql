-- Reverse of 0075. Drops the schema whole: the three tables only ever
-- reference each other, so there is nothing outside `marketing` to unpick.
DROP TABLE IF EXISTS marketing.demo_mail_outbox;
DROP TABLE IF EXISTS marketing.demo_bookings;
DROP TABLE IF EXISTS marketing.demo_requests;
DROP SCHEMA IF EXISTS marketing;
