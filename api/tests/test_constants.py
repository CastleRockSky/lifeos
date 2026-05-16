"""Tests for constants.py — domain/category integrity."""

from constants import DOMAINS, CATEGORIES, ALL_CATEGORIES, DOMAIN_CATEGORIES


def test_tax_is_a_top_level_domain():
    # Promoted out of `financial` in commit 8ba5431.
    assert "tax" in DOMAINS


def test_domains_and_category_keys_match():
    assert set(DOMAINS) == set(CATEGORIES.keys())


def test_no_duplicate_domains():
    assert len(DOMAINS) == len(set(DOMAINS))


def test_each_domain_has_categories():
    for domain, cats in CATEGORIES.items():
        assert cats, f"domain {domain!r} has no categories"


def test_no_duplicate_categories_within_a_domain():
    for domain, cats in CATEGORIES.items():
        assert len(cats) == len(set(cats)), f"duplicate category in {domain!r}"


def test_all_categories_is_flattened_view():
    expected = [cat for cats in CATEGORIES.values() for cat in cats]
    assert ALL_CATEGORIES == expected


def test_domain_categories_alias():
    assert DOMAIN_CATEGORIES is CATEGORIES
