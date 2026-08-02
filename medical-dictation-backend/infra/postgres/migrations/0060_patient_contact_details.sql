-- 0060: patient contact details — telephone, e-mail, postal address.
--
-- As built in 0031 the roster carried identity (names, DOB, sex, MRN) and
-- clinical summary, but no way to reach the patient: registering someone
-- meant capturing their phone on paper. These columns close that gap for
-- the "add patient" form.
--
-- The address is stored STRUCTURED (street / house / zip / city / country)
-- rather than as one free-text line. A single line renders fine on a card
-- but is useless the moment anything downstream needs a component of it —
-- addressing a referral letter, filtering a roster by city, handing an
-- address to a courier or laboratory integration. Splitting at capture is
-- cheap; parsing a free-text line back into parts afterwards is not, and
-- the form is the one place the components are known for certain.
--
-- Storage follows 0031's demographics decision verbatim: plaintext columns
-- protected by Postgres RLS + FORCE, exactly like `users.email` and
-- `patients.name_uk`. The envelope-encryption-at-rest boundary (ADR-0009/
-- 0010) covers audio/transcript BLOBS in object storage, not relational PII
-- columns. The ІПН is the one exception (0042) because it is a national
-- identifier used as a lookup key.
--
-- NOT NULL DEFAULT '' mirrors mrn/summary_uk: "not captured" is the empty
-- string, never NULL, so the service layer never has to branch on both.
--
-- These are PII and therefore in scope for the S11 privacy engines:
--   * DSAR export picks them up automatically — the fan-out map exports
--     `to_jsonb(x)` minus an explicit strip-list (erasure/fanout.py).
--   * The erasure tombstone must clear them; `overwrite_patient_identity`
--     in erasure/erasers.py is extended in the same change (ADR-0027 —
--     identity destroyed in place).

ALTER TABLE patients
    ADD COLUMN phone           TEXT NOT NULL DEFAULT '',
    ADD COLUMN email           TEXT NOT NULL DEFAULT '',
    ADD COLUMN address_street  TEXT NOT NULL DEFAULT '',
    ADD COLUMN address_house   TEXT NOT NULL DEFAULT '',
    ADD COLUMN address_zip     TEXT NOT NULL DEFAULT '',
    ADD COLUMN address_city    TEXT NOT NULL DEFAULT '',
    ADD COLUMN address_country TEXT NOT NULL DEFAULT '';

-- Backfill: existing rows predate the form, so there is nothing real to
-- migrate. Populate them with SYNTHETIC placeholder contacts so the pilot
-- roster renders a complete record everywhere.
--
-- Deliberately non-routable: every address is fictional and every mailbox
-- is under example.com, the RFC 2606 reserved domain — a stray notification
-- can never reach a real person. Erased tombstones are skipped (their
-- identity was destroyed on purpose) and only all-blank rows are touched,
-- so re-running after a partial apply cannot overwrite captured data.
--
-- The phone is written in the normalized form the service stores (E.164,
-- no separators — see _clean_phone in routers/patients.py) so a backfilled
-- row is indistinguishable from a captured one.
WITH ref AS (
    SELECT
        ARRAY['39','50','63','66','67','68','73','91','92','93','95','96','97','98'] AS codes,
        ARRAY['Київ', 'Львів', 'Одеса', 'Харків', 'Дніпро',
              'Вінниця', 'Полтава', 'Івано-Франківськ'] AS cities,
        ARRAY['вул. Шевченка', 'вул. Грушевського', 'просп. Свободи',
              'вул. Лесі Українки', 'вул. Сагайдачного',
              'бул. Лепкого', 'вул. Хмельницького'] AS streets
)
UPDATE patients p
   SET phone = '+380'
               || r.codes[1 + floor(random() * array_length(r.codes, 1))::int]
               || lpad(floor(random() * 10000000)::int::text, 7, '0'),
       email = 'patient.' || left(md5(p.id::text), 8) || '@example.com',
       address_street  = r.streets[1 + floor(random() * array_length(r.streets, 1))::int],
       -- The house field carries building + apartment together ("12, кв. 7"),
       -- which is how a Ukrainian street address is actually written.
       address_house   = (1 + floor(random() * 120))::int::text
                         || ', кв. ' || (1 + floor(random() * 90))::int::text,
       -- Поштовий індекс: exactly five digits, leading zeros preserved.
       address_zip     = lpad((1 + floor(random() * 99999))::int::text, 5, '0'),
       address_city    = r.cities[1 + floor(random() * array_length(r.cities, 1))::int],
       address_country = 'Україна'
  FROM ref r
 WHERE p.status <> 'erased'
   AND p.phone = ''
   AND p.email = ''
   AND p.address_street = ''
   AND p.address_city = '';
