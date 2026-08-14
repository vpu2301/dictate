"""Normalize / ngram / dedupe / fluency / phrasing / generate / release /
prompts — the mechanical pipeline stages."""

from pathlib import Path

from corpus_forge.domain.dedupe import dedupe_batch, is_near_duplicate
from corpus_forge.domain.fluency import HeuristicFluencyFilter
from corpus_forge.domain.generate import build_generation_prompt, parse_generation_batch
from corpus_forge.domain.ngram import ngrams, within_gates
from corpus_forge.domain.normalize import dedupe_key, tokenize
from corpus_forge.domain.phrasing import PhrasingTemplateSet, load_template_set, phrasify
from corpus_forge.domain.prompts import (
    ASR_PROMPT_MAX_TOKENS,
    PromptCell,
    build_prompt,
    count_tokens,
    promote_gate,
    tf_idf_terms,
)
from corpus_forge.domain.release import ReleaseRow, build_manifest

SEEDS = Path(__file__).resolve().parents[4] / "infra/seeds/corpus"


class TestNormalize:
    def test_dedupe_key_folds_case_space_apostrophe(self) -> None:
        assert dedupe_key("Сім'я  Хворого") == dedupe_key("сім’я хворого")

    def test_tokenize_keeps_hyphen_and_apostrophe_words(self) -> None:
        assert tokenize("ліво-правий шунт сім’ї") == ["ліво-правий", "шунт", "сім’ї"]


class TestNgrams:
    def test_bounds_3_to_12_tokens(self) -> None:
        grams = list(ngrams("один два три чотири"))
        assert "один два три" in grams and "один два три чотири" in grams
        assert "один два" not in grams

    def test_within_gates(self) -> None:
        assert within_gates("скарги на головний біль")
        assert not within_gates("біль")  # < 3 tokens
        assert not within_gates("а " * 45)  # > 80 chars / >12 tokens


class TestDedupe:
    def test_near_duplicate_within_3_edits(self) -> None:
        assert is_near_duplicate("стан задовільний", ["стан задовільна"])
        assert not is_near_duplicate("стан задовільний", ["стан вкрай тяжкий"])

    def test_batch_dedupes_against_corpus_and_itself(self) -> None:
        kept = dedupe_batch(
            ["скарги відсутні", "скарги відсутня", "хрипи не вислуховуються"],
            against=["хрипи не вислуховується"],
        )
        assert kept == ["скарги відсутні"]


class TestFluency:
    FILTER = HeuristicFluencyFilter()

    def test_keeps_normal_phrase(self) -> None:
        assert self.FILTER.keep("тони серця ритмічні звучні")

    def test_drops_repetition_and_mixed_script(self) -> None:
        assert not self.FILTER.keep("біль біль біль у грудях")
        assert not self.FILTER.keep("призначено aspirин після їжі")

    def test_drops_symbol_soup(self) -> None:
        assert not self.FILTER.keep("§§§ 123 --- 456 §§§")


class TestPhrasing:
    def test_templates_expand_and_respect_80_chars(self) -> None:
        ts = PhrasingTemplateSet(language="uk", section="diagnosis", templates=("{term}", "діагноз {term}"))
        assert phrasify("Гіпертонічна хвороба", ts) == [
            "Гіпертонічна хвороба",
            "діагноз Гіпертонічна хвороба",
        ]
        long_term = "х" * 79
        assert phrasify(long_term, ts) == [long_term]  # decorated variant overflows

    def test_shipped_template_sets_load(self) -> None:
        for path in sorted((SEEDS / "phrasing").glob("*/*.yaml")):
            ts = load_template_set(path)
            assert ts.templates, path


class TestGenerateParsing:
    def test_valid_array_kept_wrong_cell_dropped(self) -> None:
        raw = (
            '[{"phrase": "скарги на задишку при навантаженні", "language": "uk",'
            ' "specialty": "cardiology", "section": "anamnesis"},'
            '{"phrase": "wrong cell", "language": "en",'
            ' "specialty": "cardiology", "section": "anamnesis"}]'
        )
        parsed = parse_generation_batch(raw, language="uk", specialty="cardiology", section="anamnesis")
        assert [p.phrase for p in parsed.kept] == ["скарги на задишку при навантаженні"]
        assert parsed.dropped_malformed == 1

    def test_garbage_is_dropped_not_repaired(self) -> None:
        parsed = parse_generation_batch("no json here", language="uk", specialty="x", section="y")
        assert parsed.kept == [] and parsed.dropped_malformed == 1

    def test_prompt_carries_avoid_list_and_count(self) -> None:
        prompt = build_generation_prompt(
            language="uk", specialty="cardiology", section="plan",
            seed_terms=["бісопролол"], avoid_phrases=["продовжити прийом"], target_count=7,
        )
        assert "exactly 7" in prompt and "бісопролол" in prompt and "продовжити прийом" in prompt


class TestRelease:
    ROWS = [
        ReleaseRow("б фраза", "uk", None, None, "seed", "s", 2, "human", ()),
        ReleaseRow("а фраза", "uk", None, None, "seed", "s", 2, "human", ()),
    ]

    def test_manifest_sha_is_deterministic_and_order_independent(self) -> None:
        _, _, sha1 = build_manifest(version="v1.0.0", rows=self.ROWS, fluency_filter="heuristic-v1")
        _, _, sha2 = build_manifest(version="v1.0.0", rows=list(reversed(self.ROWS)), fluency_filter="heuristic-v1")
        assert sha1 == sha2

    def test_manifest_sha_changes_with_content(self) -> None:
        _, _, sha1 = build_manifest(version="v1.0.0", rows=self.ROWS, fluency_filter="heuristic-v1")
        _, _, sha2 = build_manifest(version="v1.0.0", rows=self.ROWS[:1], fluency_filter="heuristic-v1")
        assert sha1 != sha2


class TestPrompts:
    def test_tf_idf_finds_distinctive_terms(self) -> None:
        cells = {
            PromptCell("uk", "cardiology", None): ["фібриляція передсердь пароксизмальна форма"] * 3,
            PromptCell("uk", "general", None): ["загальний стан задовільний"] * 3,
        }
        terms = tf_idf_terms(cells, target=PromptCell("uk", "cardiology", None))
        assert "фібриляція" in terms and "стан" not in terms

    def test_build_prompt_respects_token_budget(self) -> None:
        prompt = build_prompt([f"термін{i}" for i in range(500)])
        assert count_tokens(prompt) <= ASR_PROMPT_MAX_TOKENS

    def test_promote_gate_requires_strict_improvement(self) -> None:
        assert promote_gate(incumbent_wer=0.20, candidate_wer=0.18)
        assert not promote_gate(incumbent_wer=0.20, candidate_wer=0.20)
        assert not promote_gate(incumbent_wer=0.20, candidate_wer=0.22)
