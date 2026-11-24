"""Shared rules for Cook AI: name handling, matching, diet and not-allowed checks, links, secrets.

Everything here is plain Python with no network calls, so the same rules decide
what is available, what is allowed and what is close to cookable for every user.
"""
from __future__ import annotations

import os
import re
from typing import Iterable
from urllib.parse import quote_plus

try:  # optional: lets a local .env file work when Streamlit secrets are absent
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MAX_NAME_LENGTH = 80
MAX_INGREDIENTS_PER_DISH = 30
DIETS = ("both", "veg", "non_veg", "vegan")



# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def get_secret(name: str, default: str | None = None) -> str | None:
    """Read a setting from Streamlit secrets, .streamlit/secrets.toml, or environment / .env."""
    try:
        import streamlit as st
        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass

    try:
        import tomllib
        from pathlib import Path
        p = Path(__file__).parent / ".streamlit" / "secrets.toml"
        if p.exists():
            with open(p, "rb") as f:
                s = tomllib.load(f)
                if name in s:
                    return str(s[name])
                if "." in name:
                    parts = name.split(".")
                    cur = s
                    for part in parts:
                        cur = cur.get(part, {}) if isinstance(cur, dict) else None
                    if cur is not None and not isinstance(cur, dict):
                        return str(cur)
                if name == "client_id" and "auth" in s:
                    return str(s["auth"].get("client_id", ""))
    except Exception:
        pass

    return os.getenv(name, default)


# --------------------------------------------------------------------------
# Name normalization (one function used everywhere)
# --------------------------------------------------------------------------
_QTY = re.compile(
    r"^\s*[\d./½¼¾⅓⅔]+\s*"
    r"(?:(?:cups?|tbsp|tablespoons?|tsp|teaspoons?|grams?|g|kg|ml|l|litres?|liters?|"
    r"cloves?|pieces?|slices?|pinch(?:es)?|oz|lb|lbs|cans?)\b\s*)?(?:of\s+)?"
)
_NOISE_WORDS = {
    "fresh", "chopped", "diced", "minced", "sliced", "grated",
    "large", "small", "medium", "ripe", "finely", "roughly",
}
_PLURAL_EXCEPTIONS = {
    "cookies": "cookie", "brownies": "brownie", "smoothies": "smoothie",
    "veggies": "veggie", "leaves": "leaf", "loaves": "loaf", "halves": "half",
    "knives": "knife", "chillies": "chilli", "chilies": "chilli", "chilis": "chilli",
    "anchovies": "anchovy",
}
_UNCHANGED = {"molasses", "gas"}
_WORD_ALIASES = {
    "chili": "chilli", "yoghurt": "yogurt", "aubergine": "eggplant", "courgette": "zucchini",
    "cilantro": "coriander", "capsicum": "bell pepper",
}
_PHRASE_ALIASES = {
    "garbanzo bean": "chickpea", "ground black pepper": "black pepper",
    "green onion": "spring onion", "scallion": "spring onion",
    "basmati rice": "rice", "jasmine rice": "rice", "brown rice": "rice",
    "all purpose flour": "flour", "maida": "flour",
}
_CUT_WORDS = {"breast", "thigh", "drumstick", "fillet", "leg", "wing", "loin", "chop", "steak"}
_GENERIC_HEADS = {"oil", "salt", "sugar", "flour", "rice", "pasta", "cheese", "noodle"}


def clean_name(value: object) -> str:
    """Trim, collapse spaces and cap the length of a user-entered name."""
    return " ".join(str(value or "").split())[:MAX_NAME_LENGTH]


def parse_list(text: str) -> list[str]:
    """Split 'tomato, onion; garlic' (or one per line) into separate names."""
    return [part.strip() for part in re.split(r"[,\n;]", text or "") if part.strip()]


def _singular(word: str) -> str:
    if word in _PLURAL_EXCEPTIONS:
        return _PLURAL_EXCEPTIONS[word]
    if len(word) <= 3 or not word.endswith("s") or word in _UNCHANGED:
        return word
    if word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith(("ches", "shes", "xes", "zzes")):
        return word[:-2]
    return word[:-1]


def singularize(word: str) -> str:
    return _singular(word)


def _words(text: str) -> list[str]:
    words = re.findall(r"[^\W_]+", text)
    return [w for w in words if not (len(w) == 1 and w.isalpha())]


def normalize_key(value: object) -> str:
    """Lowercase, drop quantities and prep words, singularize, apply a few aliases.

    'Tomatoes', '2 tomato' and 'tomato, chopped' all become 'tomato'.
    """
    text = str(value or "").casefold()
    text = re.sub(r"\(.*?\)", " ", text)
    text = text.split(",")[0]
    text = _QTY.sub("", text)
    words = _words(text)
    kept = [w for w in words if w not in _NOISE_WORDS] or words
    if not kept:
        return ""
    kept[-1] = _singular(kept[-1])
    kept = [_WORD_ALIASES.get(w, w) for w in kept]
    key = " ".join(kept)
    return _PHRASE_ALIASES.get(key, key)


def key_set(names: Iterable[str]) -> set[str]:
    return {key for key in (normalize_key(n) for n in names) if key}


def clean_ingredient_names(names: Iterable[object] | None) -> list[str]:
    """Clean a list of ingredient names, drop blanks and duplicates, keep order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = clean_name(raw)
        key = normalize_key(name)
        if name and key and key not in seen:
            out.append(name)
            seen.add(key)
    return out[:MAX_INGREDIENTS_PER_DISH]


# --------------------------------------------------------------------------
# Availability and matching
# --------------------------------------------------------------------------
def is_available(key: str, inventory_keys: set[str]) -> bool:
    """True if an ingredient key is covered by the user's inventory.

    Exact match, or a harmless variant: 'chicken breast' when the user has
    'chicken' (and the reverse), or a generic word such as 'oil' when the user
    has 'olive oil'. Anything else (for example 'pepper' vs 'bell pepper') is
    deliberately NOT treated as available.
    """
    if not key:
        return False
    if key in inventory_keys:
        return True
    words = key.split()
    if len(words) > 1 and words[0] in inventory_keys and all(w in _CUT_WORDS for w in words[1:]):
        return True
    if len(words) == 1:
        for inv in inventory_keys:
            inv_words = inv.split()
            if len(inv_words) > 1 and inv_words[0] == key and all(w in _CUT_WORDS for w in inv_words[1:]):
                return True
        if key in _GENERIC_HEADS and any(inv.split()[-1] == key for inv in inventory_keys):
            return True
    return False


def missing_from(ingredients: Iterable[str], inventory_keys: set[str]) -> list[str]:
    """Ingredient names that the inventory does not cover (deduplicated)."""
    out: list[str] = []
    seen: set[str] = set()
    for name in ingredients:
        key = normalize_key(name)
        if key and key not in seen and not is_available(key, inventory_keys):
            out.append(name)
            seen.add(key)
    return out


# --------------------------------------------------------------------------
# Diet (veg / non-veg / both). Eggs count as non-veg.
# --------------------------------------------------------------------------
_NON_VEG_WORDS = {
    "chicken", "beef", "pork", "lamb", "mutton", "goat", "turkey", "duck", "bacon", "ham",
    "sausage", "salami", "pepperoni", "chorizo", "steak", "mince", "meat", "liver", "bone",
    "fish", "salmon", "tuna", "cod", "sardine", "anchovy", "mackerel", "trout", "tilapia",
    "shrimp", "prawn", "crab", "lobster", "squid", "calamari", "octopus", "mussel", "clam",
    "oyster", "scallop", "egg", "gelatin", "gelatine", "lard", "mayonnaise", "mayo",
    "worcestershire",
}


_DAIRY_AND_ANIMAL_DERIVATIVES = {
    "milk", "butter", "cheese", "yogurt", "curd", "cream", "ghee", "paneer",
    "whey", "casein", "gelatin", "gelatine", "lard", "honey", "mayo", "mayonnaise",
    "mozzarella", "parmesan", "cheddar", "feta", "ricotta", "custard",
}
_PLANT_MILK_WORDS = {"coconut", "almond", "soy", "oat", "rice", "cashew", "hemp"}


def is_non_veg(ingredient: str) -> bool:
    tokens = {_singular(w) for w in normalize_key(ingredient).split()}
    if "mushroom" in tokens:
        tokens.discard("oyster")
    if "cheese" in tokens or "milk" in tokens:
        tokens.discard("goat")
    return bool(tokens & _NON_VEG_WORDS)


def is_dairy_or_animal(ingredient: str) -> bool:
    """True if ingredient contains dairy, honey, gelatin, or animal fat."""
    tokens = {_singular(w) for w in normalize_key(ingredient).split()}
    # Allow plant milks (e.g. coconut milk, almond milk)
    if "milk" in tokens and bool(tokens & _PLANT_MILK_WORDS):
        tokens.discard("milk")
    return bool(tokens & _DAIRY_AND_ANIMAL_DERIVATIVES)


def is_vegan(ingredient: str) -> bool:
    return not is_non_veg(ingredient) and not is_dairy_or_animal(ingredient)


def diet_allows(ingredients: Iterable[str], diet: str) -> bool:
    """'vegan': 100% plant-based. 'veg': no meat/fish/egg. 'non_veg': at least one. 'both': anything."""
    contains_non_veg = any(is_non_veg(i) for i in ingredients)
    if diet == "vegan":
        return not contains_non_veg and all(not is_dairy_or_animal(i) for i in ingredients)
    if diet == "veg":
        return not contains_non_veg
    if diet == "non_veg":
        return contains_non_veg
    return True


# --------------------------------------------------------------------------
# Not-allowed items (conservative on purpose: 'milk' also blocks 'coconut milk')
# --------------------------------------------------------------------------
def _token_forms(text: str) -> set[str]:
    forms = set()
    for word in _words(str(text or "").casefold()):
        word = _singular(word)
        forms.add(_WORD_ALIASES.get(word, word))
    return forms


def hits_prohibited(text: str, prohibited_keys: Iterable[str]) -> list[str]:
    """Not-allowed items found in an ingredient name or in free text such as a recipe step."""
    tokens = _token_forms(text)
    return [p for p in prohibited_keys if p and set(p.split()) <= tokens]


# --------------------------------------------------------------------------
# Favourite matching: Python decides, the AI never does
# --------------------------------------------------------------------------
def classify_dishes(
    dishes: list[dict],
    inventory_keys: set[str],
    prohibited_keys: Iterable[str],
    diet: str,
    use_soon_keys: Iterable[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Split saved dishes into (cookable now, close: missing 1-2 ingredients).

    Dishes without ingredients, with a not-allowed item, or against the chosen
    diet are dropped. Dishes using 'use soon' ingredients are ranked first.
    """
    prohibited = list(prohibited_keys)
    use_soon = set(use_soon_keys)
    cookable: list[dict] = []
    near: list[dict] = []
    for dish in dishes:
        ingredients = dish.get("ingredients") or []
        if not ingredients:
            continue
        if hits_prohibited(dish.get("name", ""), prohibited):
            continue
        if any(hits_prohibited(i, prohibited) for i in ingredients):
            continue
        if not diet_allows(ingredients, diet):
            continue
        missing = missing_from(ingredients, inventory_keys)
        entry = {
            **dish,
            "missing": missing,
            "use_soon_count": sum(1 for i in ingredients if normalize_key(i) in use_soon),
        }
        if not missing:
            cookable.append(entry)
        elif len(missing) <= 2:
            near.append(entry)
    cookable.sort(key=lambda d: (-d["use_soon_count"], d.get("key", "")))
    near.sort(key=lambda d: (len(d["missing"]), -d["use_soon_count"], d.get("key", "")))
    return cookable, near


# --------------------------------------------------------------------------
# Links are built in code, never written by the AI
# --------------------------------------------------------------------------
def youtube_url(dish_name: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(f"{dish_name} recipe")


def recipe_url(dish_name: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(f"{dish_name} recipe")
