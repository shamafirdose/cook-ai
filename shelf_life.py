"""Shelf life estimations and food rescue valuation for Cook AI.

Provides category-based shelf-life estimates for fresh produce, proteins, dairy,
and pantry staples so users can track expiring items and see estimated money saved.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

from logic import normalize_key, singularize

# Default shelf-life in days by food category keywords
_CATEGORY_SHELF_LIFE = {
    # 2 days: Fresh meats, poultry, seafood, fresh berries
    ("chicken", "turkey", "beef", "pork", "fish", "salmon", "shrimp", "prawn",
     "tuna", "mince", "ground beef", "meat", "scallop", "berry", "strawberry", "raspberry", "blackberry"): (2, "Protein / Berries"),
    
    # 3-4 days: Leafy greens, herbs, cut mushrooms, cooked leftovers, paneer, tofu
    ("spinach", "lettuce", "kale", "coriander", "cilantro", "parsley", "basil",
     "mint", "mushroom", "paneer", "tofu", "cream", "ricotta", "sprout", "bean sprout"): (4, "Fresh Greens / Herbs"),
    
    # 7 days: Most fresh vegetables, soft cheeses, milk, bread
    ("tomato", "cucumber", "zucchini", "capsicum", "bell pepper", "broccoli",
     "cauliflower", "cabbage", "carrot", "celery", "milk", "bread", "bun", "avocado",
     "eggplant", "aubergine", "green bean", "pea"): (7, "Fresh Produce"),
    
    # 14-21 days: Citrus, eggs, firm cheeses, yogurt
    ("egg", "lemon", "lime", "orange", "apple", "yogurt", "yoghurt", "curd",
     "cheddar", "parmesan", "mozzarella", "butter"): (14, "Dairy / Citrus"),
    
    # 30-60 days: Root vegetables, garlic, onions, ginger, squash, potatoes
    ("potato", "onion", "garlic", "ginger", "shallot", "pumpkin", "squash",
     "sweet potato", "beetroot"): (45, "Root Vegetables"),
    
    # 180+ days: Dry pantry staples, rice, flour, pulses, spices, oils
    ("rice", "flour", "pasta", "noodle", "lentil", "dal", "chickpea", "bean",
     "oil", "salt", "sugar", "pepper", "cumin", "turmeric", "soy sauce",
     "vinegar", "honey", "oat", "canned"): (180, "Pantry Staple"),
}

# Estimated average replacement cost ($) for an expiring ingredient portion
DEFAULT_INGREDIENT_VALUE = 3.50


def estimate_shelf_life(ingredient_name: str) -> dict:
    """Returns estimated days, category label, and default expiration date."""
    key = normalize_key(ingredient_name)
    tokens = {singularize(w) for w in key.split()}
    
    days = 7  # general fallback for fresh ingredients
    category = "General Fresh"
    
    for keywords, (cat_days, cat_label) in _CATEGORY_SHELF_LIFE.items():
        if any(kw in key or kw in tokens for kw in keywords):
            days = cat_days
            category = cat_label
            break
            
    today = dt.date.today()
    expires_at = (today + dt.timedelta(days=days)).strftime("%Y-%m-%d")
    
    return {
        "days": days,
        "category": category,
        "expires_at": expires_at,
        "is_perishable": days <= 14,
    }


def days_until(expires_at_str: str | None) -> int | None:
    """Calculate days remaining until expiry. Negative if already expired."""
    if not expires_at_str:
        return None
    try:
        exp = dt.date.fromisoformat(expires_at_str[:10])
        return (exp - dt.date.today()).days
    except Exception:
        return None


def get_expiry_status(expires_at_str: str | None) -> dict:
    """Returns a status badge description: urgent, soon, fresh, or unknown."""
    days = days_until(expires_at_str)
    if days is None:
        return {"status": "none", "label": "", "color": "gray"}
    if days < 0:
        return {"status": "expired", "label": "Expired", "color": "red"}
    if days <= 2:
        return {"status": "urgent", "label": f"{days}d left! ⚠️", "color": "red"}
    if days <= 4:
        return {"status": "soon", "label": f"{days}d left", "color": "amber"}
    return {"status": "good", "label": f"{days}d", "color": "green"}


def calculate_rescued_value(item_count: int) -> float:
    """Approximate money saved by cooking expiring ingredients instead of throwing them away."""
    return round(item_count * DEFAULT_INGREDIENT_VALUE, 2)
