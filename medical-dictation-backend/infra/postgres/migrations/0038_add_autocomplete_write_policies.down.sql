-- Reverse 0038: drop the PERMISSIVE app_role write policies (returns the
-- write API to the deny-all state of 0023/0024).

DROP POLICY IF EXISTS app_insert_phrases ON autocomplete_phrases;
DROP POLICY IF EXISTS app_update_phrases ON autocomplete_phrases;
DROP POLICY IF EXISTS app_insert_snippets ON autocomplete_snippets;
DROP POLICY IF EXISTS app_update_snippets ON autocomplete_snippets;
