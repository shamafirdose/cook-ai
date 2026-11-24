"""Cook AI - pantry and meal suggestions. Streamlit UI, Google sign-in, one private kitchen per user."""
from __future__ import annotations

import html
import re
import time

import streamlit as st

import database as db
import gemini_client as ai
import logic as L
import photos

st.set_page_config(page_title="Cook AI", page_icon="🍳", layout="centered", initial_sidebar_state="collapsed")

DAILY_LIMIT = int(L.get_secret("DAILY_SUGGESTIONS", "10") or 10)
MAX_PROPOSALS_PER_SESSION = 25
DIET_LABELS = {"veg": "🥦 Veg", "non_veg": "🍗 Non-veg", "both": "🍽️ Both"}
NAV_TABS = ("🍳 Cook", "🧺 Kitchen", "🕘 History")
KITCHEN_VIEWS = ("Pantry", "Home recipes", "Restricted")
COMMON_ESSENTIALS = ["salt", "black pepper", "cooking oil", "water", "sugar", "cumin", "turmeric",
                     "chilli powder", "paprika", "oregano", "garlic powder", "soy sauce", "vinegar","butter","ghee","coriander powder","cinnamon","cloves","lemon","milk","yogurt"]

st.markdown(
    """
    <style>
    .block-container { max-width: 820px; padding-top: 3.2rem; padding-bottom: 7rem; }
    @media (max-width: 640px) { .block-container { padding-left: .8rem; padding-right: .8rem; } }
    [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 16px; }
    div.stButton > button, div.stLinkButton > a, div.stFormSubmitButton > button { border-radius: 12px; min-height: 2.6rem; font-weight: 600; }
    [data-testid="stImage"] img { border-radius: 12px; }
    .cook-chip, .cook-miss, .cook-tag, .cook-time { display: inline-block; padding: .18rem .6rem; margin: .12rem .2rem .12rem 0;
        border-radius: 999px; font-size: .8rem; font-weight: 600; }
    .cook-chip { background: rgba(31,61,43,.12); }
    .cook-miss { background: rgba(224,160,48,.30); }
    .cook-tag  { background: rgba(224,160,48,.22); font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
    .cook-time { background: rgba(120,120,120,.10); }
    .cook-hint { opacity: .75; font-size: .9rem; margin: -.3rem 0 .8rem 0; }
    .cook-ph { height: 7rem; border-radius: 12px; background: rgba(224,160,48,.15); display: flex;
        align-items: center; justify-content: center; font-size: 2.4rem; margin-bottom: .4rem; }
    .st-key-topbar [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; align-items: center; gap: .5rem; }
    .st-key-topbar [data-testid="stColumn"] { min-width: 0 !important; width: auto !important; flex: 0 0 auto !important; }
    .st-key-topbar [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
    .st-key-bottomnav { position: fixed; bottom: 0; left: 0; right: 0; z-index: 999; background: #ffffff;
        border-top: 1px solid rgba(0,0,0,.10); padding: .6rem 1rem calc(.6rem + env(safe-area-inset-bottom)); }
    .st-key-bottomnav > div { max-width: 820px; margin: 0 auto; }
    .st-key-bottomnav [data-testid="stElementContainer"] { width: 100% !important; }
    .st-key-bottomnav [data-testid="stButtonGroup"] { width: 100%; }
    .st-key-bottomnav [role="radiogroup"] { width: 100% !important; max-width: none !important; display: flex !important; gap: .4rem; }
    .st-key-bottomnav [role="radiogroup"] button { flex: 1 1 0 !important; width: auto !important; min-width: 0 !important; min-height: 3.2rem; padding: .4rem .3rem;
        border-radius: 12px; justify-content: center; }
    .st-key-bottomnav [role="radiogroup"] button > div,
    .st-key-bottomnav [role="radiogroup"] button span { max-width: none !important; overflow: visible !important;
        text-overflow: clip !important; }
    .st-key-bottomnav [role="radiogroup"] button p { white-space: nowrap; font-size: 1.05rem; font-weight: 600; }
    .st-key-bottomnav [data-testid="stIconEmoji"] { font-size: 1.25rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def esc(text: object) -> str:
    return html.escape(str(text))


def md_safe(text: object) -> str:
    """Escape Markdown so user or AI text is shown as plain text."""
    return re.sub(r"([\\`*_{}\[\]()#+!|<>~$])", r"\\\1", str(text))


def rev() -> int:
    return st.session_state.setdefault("rev", 0)


def flash(message: str, icon: str | None = None) -> None:
    st.session_state["flash"] = (message, icon)


def changed() -> None:
    """The user's kitchen changed: forget old suggestions and refresh every widget."""
    st.session_state.pop("results", None)
    st.session_state["rev"] = rev() + 1
    st.rerun()


def chips(names: list[str], miss_keys: set[str] | None = None, stars: set[str] | None = None) -> str:
    miss_keys, stars = miss_keys or set(), stars or set()
    parts = []
    for name in names:
        key = L.normalize_key(name)
        cls = "cook-miss" if key in miss_keys else "cook-chip"
        parts.append(f'<span class="{cls}">{"⭐ " if key in stars else ""}{esc(name)}</span>')
    return "".join(parts)


def pick(label: str, options, *, key: str, default, **kwargs) -> str:
    """A segmented control that always returns a value, even if the user taps the active option again."""
    value = st.segmented_control(label, options, default=default, key=key, label_visibility="collapsed", **kwargs)
    if value is None:
        return st.session_state.get(f"{key}_last", default)
    st.session_state[f"{key}_last"] = value
    return value


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------
def auth_configured() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def get_user() -> tuple[str, str, bool]:
    """Returns (user_id, display_name, test_mode). Stops the page if nobody is signed in."""
    if auth_configured():
        if not st.user.is_logged_in:
            _, middle, _ = st.columns([1, 2, 1])
            with middle:
                st.markdown("# 🍳 Cook AI")
                st.markdown("Tell Cook AI what is in your kitchen and get meal ideas from what you already have.")
                st.button("Continue with Google", type="primary", width="stretch", on_click=st.login)
                st.caption("Your pantry and dishes are private to your account.")
            st.stop()
        user_id = st.user.get("sub") or st.user.get("email")
        if not user_id:
            st.error("Sign-in did not return an account id. Please sign out and try again.")
            st.stop()
        return str(user_id), str(st.user.get("name") or "Cook"), False
    dev_id = L.get_secret("DEV_USER_ID")
    if dev_id:
        return dev_id, "Test user", True
    st.error("Sign-in is not configured yet. Add the [auth] section to your Streamlit secrets (see README).")
    st.stop()


# --------------------------------------------------------------------------
# Result cards
# --------------------------------------------------------------------------
@st.dialog("Cooked this?")
def cooked_dialog(uid: str, recipe: dict, pantry: list[dict]) -> None:
    st.markdown(f"**{md_safe(recipe['name'])}**")
    rkeys = [L.normalize_key(i) for i in recipe["ingredients"]]
    options = {p["id"]: p["name"] for p in pantry
               if any(rk == p["key"] or L.is_available(rk, {p["key"]}) for rk in rkeys)}
    used_up: list[int] = []
    if options:
        st.caption("Optional: tick anything you used up and it will be removed from your pantry.")
        used_up = st.multiselect("Used up", list(options), format_func=options.get, label_visibility="collapsed")
    if st.button("Save to my history", type="primary", width="stretch"):
        db.add_history(uid, recipe["name"], recipe["source"])
        if used_up:
            db.delete_items(uid, "pantry", used_up)
            st.session_state.pop("results", None)
            st.session_state["rev"] = rev() + 1
        flash("Saved. Enjoy your meal! 🍽️")
        st.rerun()


def photo_block(name: str) -> None:
    if not photos.photos_configured():
        return
    photo = photos.get_photo(name)
    if photo:
        st.image(photo["url"], width="stretch")
    else:
        st.markdown('<div class="cook-ph">🍽️</div>', unsafe_allow_html=True)


def recipe_card(uid: str, r: dict, key: str, pantry: list[dict]) -> None:
    tag = {"favourite": "Home recipe", "near_match": "Almost there", "alternative": "New idea"}[r["source"]]
    with st.container(border=True):
        photo_block(r["name"])
        minutes = f'<span class="cook-time">⏱ {int(r["minutes"])} min</span>' if r.get("minutes") else ""
        st.markdown(f'<span class="cook-tag">{tag}</span> {minutes}', unsafe_allow_html=True)
        st.markdown(f"#### {md_safe(r['name'])}")
        missing = r.get("missing") or []
        st.markdown(chips(r["ingredients"], miss_keys=L.key_set(missing)), unsafe_allow_html=True)
        if missing:
            st.markdown("🛒 You need: **" + ", ".join(md_safe(m) for m in missing) + "**")
        if r["has_steps"]:
            with st.expander("Steps"):
                for number, step in enumerate(r["steps"], start=1):
                    st.markdown(f"**{number}.** {md_safe(step)}")
        else:
            st.caption("Steps are not available right now. Use the links below.")
        left, right = st.columns(2)
        left.link_button("▶ YouTube", L.youtube_url(r["name"]), width="stretch")
        right.link_button("📖 Recipe", L.recipe_url(r["name"]), width="stretch")
        a, b = st.columns(2)
        if a.button("✅ Cooked this", key=f"{key}_cooked", width="stretch"):
            cooked_dialog(uid, r, pantry)
        if r["source"] == "alternative" and b.button("⭐ Save", key=f"{key}_save", width="stretch", help="Save to your home recipes"):
            status = db.add_dish(uid, r["name"], r["ingredients"])
            flash({"ok": "Saved to your home recipes ⭐", "exists": "Already in your home recipes"}.get(status, "Could not save this dish"))
            st.rerun()
        if r["source"] == "near_match" and missing and b.button("🛒 Add to list", key=f"{key}_shop", width="stretch"):
            added = db.add_many(uid, "shopping", missing)
            flash(f"Added {len(added['added'])} item(s) to your shopping list" if added["added"] else "Already on your list", "🛒")
            st.rerun()


def grid(uid: str, recipes: list[dict], section: str, pantry: list[dict]) -> None:
    columns = st.columns(2)
    for index, recipe in enumerate(recipes):
        with columns[index % 2]:
            recipe_card(uid, recipe, f"{section}_{index}", pantry)


def render_results(uid: str, res: dict, data: dict) -> None:
    if res["status"] in ("not_food", "empty"):
        st.info(res["message"])
        return
    if res["message"]:
        st.warning(res["message"])
    for note in res["notes"]:
        st.caption(note)

    shown = [r["name"] for r in res["favourites"] + res["alternatives"] + ([res["near"]] if res["near"] else [])]
    photos.prefetch(shown)

    st.markdown("### 🍲 Your home recipes you can cook now")
    if res["favourites"]:
        grid(uid, res["favourites"], "fav", data["pantry"])
    elif not data["dishes"]:
        st.info("Save a few dishes you often cook (My kitchen → Home recipes) and they will show up here.")
    else:
        st.info("None of your home recipes can be made with what you have right now.")

    st.markdown("### ✨ Try something different")
    if res["alternatives"]:
        grid(uid, res["alternatives"], "alt", data["pantry"])
    else:
        st.info("No new ideas this time. Try again or add more ingredients.")

    st.markdown("### 🛒 One step away")
    if res["near"]:
        grid(uid, [res["near"]], "near", data["pantry"])
    else:
        st.info("No home recipe is only 1-2 ingredients away right now.")


def run_suggestions(uid: str, data: dict, meal: str, prompt: str) -> bool:
    """Returns True when a suggestion request was counted."""
    inventory = [i["name"] for i in data["pantry"] + data["essential"]]
    if not data["pantry"]:
        st.warning("Add what you have at home first (My kitchen → Pantry).")
        return False
    allowed, _used = db.consume_suggestion(uid, DAILY_LIMIT)
    if not allowed:
        st.warning(f"You have used all {DAILY_LIMIT} suggestions for today. Come back tomorrow!")
        return False
    prohibited_keys = [i["key"] for i in data["prohibited"]]
    use_soon = [i["key"] for i in data["pantry"] if i["use_soon"]]
    cookable, near = L.classify_dishes(
        data["dishes"], L.key_set(inventory), prohibited_keys, data["diet"], use_soon)
    try:
        history = [h["name"] for h in db.get_recent_history(uid, 15)]
    except Exception:
        history = []
    with st.spinner("Cooking up ideas…"):
        st.session_state["results"] = ai.generate_suggestions(
            inventory_names=inventory, prohibited_names=[i["name"] for i in data["prohibited"]],
            diet=data["diet"], meal=meal, user_prompt=prompt, cookable=cookable, near=near,
            favourite_names=[d["name"] for d in data["dishes"]], history_names=history)
    return True


# --------------------------------------------------------------------------
# My kitchen: lists
# --------------------------------------------------------------------------
def add_names(uid: str, kind: str, names: list[str]) -> None:
    if not names:
        st.warning("Type at least one name.")
        return
    result = db.add_many(uid, kind, names[:30])
    text = f"Added {len(result['added'])}" if result["added"] else "Nothing new to add"
    if result["skipped"]:
        text += f" ({len(result['skipped'])} already there or invalid)"
    flash(text)
    changed()


def list_section(uid: str, kind: str, items: list[dict], *, placeholder: str, quick: list[str] | None = None,
                 starred: bool = False) -> None:
    r = rev()
    with st.form(f"add_{kind}_{r}", clear_on_submit=True, border=False):
        text = st.text_input("Add", placeholder=placeholder, max_chars=300, label_visibility="collapsed")
        if st.form_submit_button("＋ Add", width="stretch"):
            add_names(uid, kind, L.parse_list(text))
    if quick:
        have = {i["key"] for i in items}
        options = [q for q in quick if L.normalize_key(q) not in have]
        if options:
            picked = st.pills("Quick add", options, selection_mode="multi", key=f"quick_{kind}_{r}")
            if picked and st.button("Add selected", key=f"quick_btn_{kind}_{r}", width="stretch"):
                add_names(uid, kind, list(picked))
    if not items:
        st.caption("Nothing here yet.")
        return
    star_keys = {i["key"] for i in items if i.get("use_soon")}
    st.markdown(chips([i["name"] for i in items], stars=star_keys), unsafe_allow_html=True)
    names = {i["id"]: i["name"] for i in items}
    with st.popover("✏️ Edit or remove", width="stretch"):
        remove = st.multiselect("Remove", list(names), format_func=names.get, key=f"rm_{kind}_{r}")
        if remove and st.button("Remove selected", key=f"rm_btn_{kind}_{r}", width="stretch"):
            db.delete_items(uid, kind, remove)
            flash(f"Removed {len(remove)}")
            changed()
        st.divider()
        target = st.selectbox("Rename", list(names), format_func=names.get, key=f"rn_sel_{kind}_{r}")
        new_name = st.text_input("New name", max_chars=80, key=f"rn_new_{kind}_{r}")
        if st.button("Rename", key=f"rn_btn_{kind}_{r}", width="stretch"):
            status = db.rename_item(uid, kind, target, new_name)
            if status == "ok":
                flash("Renamed")
                changed()
            st.warning({"exists": "That name already exists.", "empty": "Type a new name."}.get(status, "Could not rename."))
        if starred:
            st.divider()
            wanted = st.multiselect("⭐ Use these up first", list(names), format_func=names.get,
                                    default=[i["id"] for i in items if i.get("use_soon")], key=f"soon_{kind}_{r}")
            if st.button("Save ⭐", key=f"soon_btn_{kind}_{r}", width="stretch"):
                for item in items:
                    now = item["id"] in wanted
                    if now != item.get("use_soon"):
                        db.set_use_soon(uid, item["id"], now)
                flash("Saved ⭐")
                changed()


def _toggle_bought(uid: str, item_id: int, key: str) -> None:
    db.set_bought(uid, item_id, bool(st.session_state.get(key)))


def shopping_section(uid: str, items: list[dict]) -> None:
    r = rev()
    st.markdown("**🛒 Shopping list**")
    with st.form(f"add_shopping_{r}", clear_on_submit=True, border=False):
        text = st.text_input("Add", placeholder="e.g. paneer, coriander", max_chars=300, label_visibility="collapsed")
        if st.form_submit_button("＋ Add", width="stretch"):
            names = L.parse_list(text)
            if names:
                db.add_many(uid, "shopping", names[:30])
                st.session_state["rev"] = r + 1
                st.rerun()
    if not items:
        st.caption("Your shopping list is empty. Add missing ingredients from the 'One step away' card.")
        return
    for item in items:
        key = f"shop_{item['id']}_{r}"
        st.checkbox(item["name"], value=item["bought"], key=key, on_change=_toggle_bought, args=(uid, item["id"], key))
    bought = [i for i in items if i["bought"]]
    left, right = st.columns(2)
    if left.button("📦 To pantry", key=f"shop_move_{r}", disabled=not bought, width="stretch",
                   help="Move bought items to your pantry"):
        for item in bought:
            db.move_shopping_to_pantry(uid, item["id"])
        flash(f"Moved {len(bought)} to your pantry")
        changed()
    if right.button("🗑 Clear bought", key=f"shop_clear_{r}", disabled=not bought, width="stretch"):
        db.delete_items(uid, "shopping", [i["id"] for i in bought])
        st.session_state["rev"] = r + 1
        st.rerun()
    st.caption("Copy the list:")
    st.code("\n".join(i["name"] for i in items if not i["bought"]) or "(all bought)", language=None)


# --------------------------------------------------------------------------
# My kitchen: home recipes (saved dishes)
# --------------------------------------------------------------------------
def start_draft(name: str, dishes: list[dict]) -> None:
    name = L.clean_name(name)
    key = L.normalize_key(name)
    if not key:
        st.warning("Type a dish name.")
        return
    if any(d["key"] == key for d in dishes):
        st.warning(f"'{name}' is already in your home recipes.")
        return
    count = st.session_state.get("proposals", 0)
    if count >= MAX_PROPOSALS_PER_SESSION:
        proposal = {"ok": False, "ingredients": [], "message": "AI ingredient help is paused for now. Type the ingredients yourself."}
    else:
        st.session_state["proposals"] = count + 1
        with st.spinner("Finding the main ingredients…"):
            proposal = ai.propose_dish_ingredients(name)
    st.session_state["draft"] = {"id": int(time.time() * 1000), "name": name,
                                 "options": proposal["ingredients"], "message": proposal["message"]}
    st.rerun()


def render_draft(uid: str, draft: dict) -> None:
    with st.container(border=True):
        st.markdown(f"**{md_safe(draft['name'])}**")
        chosen: list[str] = []
        if draft["options"]:
            st.caption("Tap an ingredient to switch it off if you don't use it.")
            chosen = list(st.pills("Ingredients", draft["options"], selection_mode="multi",
                                   default=draft["options"], key=f"draft_{draft['id']}", label_visibility="collapsed") or [])
        else:
            st.info(draft["message"] or "Type the main ingredients below.")
        extra = st.text_input("Add other ingredients (comma separated)", max_chars=300, key=f"draft_extra_{draft['id']}")
        save, cancel = st.columns(2)
        if save.button("💾 Save dish", type="primary", key=f"draft_save_{draft['id']}", width="stretch"):
            status = db.add_dish(uid, draft["name"], chosen + L.parse_list(extra))
            st.session_state.pop("draft", None)
            flash({"ok": "Dish saved ⭐", "exists": "Already saved", "limit": "You reached the dish limit"}.get(status, "Could not save"))
            changed()
        if cancel.button("Cancel", key=f"draft_cancel_{draft['id']}", width="stretch"):
            st.session_state.pop("draft", None)
            st.rerun()


def dishes_section(uid: str, dishes: list[dict]) -> None:
    r = rev()
    st.markdown('<div class="cook-hint">Save the dishes you cook often. Cook AI suggests the main ingredients, you just tap.</div>',
                unsafe_allow_html=True)
    draft = st.session_state.get("draft")
    if draft:
        render_draft(uid, draft)
    else:
        with st.form(f"new_dish_{r}", clear_on_submit=True, border=False):
            name = st.text_input("Dish name", placeholder="e.g. Chicken biryani", max_chars=80, label_visibility="collapsed")
            go = st.form_submit_button("✨ Find ingredients", width="stretch")
        if go:
            start_draft(name, dishes)
    if not dishes:
        st.caption("No home recipes yet.")
        return
    for dish in dishes:
        if dish["status"] == "pending" or not dish["ingredients"]:
            st.markdown(f"• **{md_safe(dish['name'])}** ⚠️ _no ingredients yet_")
        else:
            st.markdown(f"• **{md_safe(dish['name'])}** — {md_safe(', '.join(dish['ingredients']))}")
    names = {d["id"]: d["name"] for d in dishes}
    with st.popover("✏️ Edit or remove a dish", width="stretch"):
        dish_id = st.selectbox("Dish", list(names), format_func=names.get, key=f"dish_sel_{r}")
        dish = next(d for d in dishes if d["id"] == dish_id)
        new_name = st.text_input("Name", value=dish["name"], max_chars=80, key=f"dish_name_{dish_id}_{r}")
        text = st.text_area("Ingredients (comma separated)", value=", ".join(dish["ingredients"]), max_chars=600,
                            key=f"dish_ing_{dish_id}_{r}")
        if st.button("Save changes", type="primary", key=f"dish_save_{dish_id}_{r}", width="stretch"):
            status = db.update_dish(uid, dish_id, new_name, L.parse_list(text))
            if status == "ok":
                flash("Dish updated")
                changed()
            st.warning({"exists": "Another dish already has that name.", "empty": "Type a name."}.get(status, "Could not save."))
        if st.button("🪄 Suggest ingredients with AI", key=f"dish_ai_{dish_id}_{r}", width="stretch"):
            with st.spinner("Finding ingredients…"):
                proposal = ai.propose_dish_ingredients(dish["name"])
            if proposal["ok"]:
                db.set_dish_ingredients(uid, dish_id, proposal["ingredients"])
                flash("Ingredients updated")
                changed()
            st.warning(proposal["message"])
        if st.button("🗑 Delete this dish", key=f"dish_del_{dish_id}_{r}", width="stretch"):
            db.delete_dish(uid, dish_id)
            flash("Dish deleted")
            changed()


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
def render_cook(uid: str, data: dict) -> None:
    diet = pick("Diet", list(DIET_LABELS), key="diet_choice", default=data["diet"], format_func=DIET_LABELS.get)
    if diet != data["diet"]:
        db.set_diet(uid, diet)
        changed()

    prompt = st.text_input("Request", max_chars=ai.MAX_PROMPT_CHARS, key="prompt", label_visibility="collapsed",
                           placeholder="Anything specific? e.g. something spicy under 20 minutes")
    meal = pick("Meal", list(ai.MEALS), key="meal", default="Any")

    ran = st.button("✨ Suggest", type="primary", width="stretch")
    used_today = data["usage_today"]
    if ran and run_suggestions(uid, data, meal, prompt):
        used_today += 1
    st.caption(f"{min(used_today, DAILY_LIMIT)} of {DAILY_LIMIT} suggestions used today")

    if not ai.ai_configured():
        st.warning("The AI key is missing (GEMINI_API_KEY), so only saved dishes can be shown.")
    if results := st.session_state.get("results"):
        render_results(uid, results, data)


def render_pantry(uid: str, data: dict) -> None:
    if not data["pantry"]:
        st.info("Start here: add what you have at home. Add salt, oil, water and spices under **Kitchen essentials**. "
                "Cook AI never assumes you have them.")
    with st.expander(f"🧂 Kitchen essentials ({len(data['essential'])})"):
        st.markdown('<div class="cook-hint">Oil, salt, water, spices and sauces you always keep.</div>', unsafe_allow_html=True)
        list_section(uid, "essential", data["essential"], placeholder="e.g. salt, cooking oil, cumin", quick=COMMON_ESSENTIALS)
    st.markdown(f"**🥬 Pantry ({len(data['pantry'])})**")
    st.markdown('<div class="cook-hint">Fresh and stored food at home. Several at once: "tomato, onion, rice". '
                'Tap ✏️ to star what to use up first.</div>', unsafe_allow_html=True)
    list_section(uid, "pantry", data["pantry"], placeholder="e.g. chicken, tomato, rice", starred=True)


def render_restricted(uid: str, data: dict) -> None:
    st.markdown(f"**🚫 Restricted ({len(data['prohibited'])})**")
    st.markdown('<div class="cook-hint">Never suggested, in any dish or step. Allergies, dislikes, anything you avoid. '
                'Matching is careful: "milk" also blocks "coconut milk".</div>', unsafe_allow_html=True)
    list_section(uid, "prohibited", data["prohibited"], placeholder="e.g. mushroom, peanut")


def render_kitchen(uid: str, data: dict) -> None:
    st.markdown("### 🧺 My kitchen")
    view = pick("My kitchen section", list(KITCHEN_VIEWS), key="kitchen_view", default="Pantry")
    if view == "Pantry":
        render_pantry(uid, data)
    elif view == "Home recipes":
        dishes_section(uid, data["dishes"])
    else:
        render_restricted(uid, data)


def render_history(uid: str) -> None:
    st.markdown("### 🕘 History")
    history = db.get_recent_history(uid, 30)
    if not history:
        st.info("Nothing yet. Tap “Cooked this” on a recipe and it will show up here.")
        return
    for h in history:
        with st.container(border=True):
            st.markdown(f"**{md_safe(h['name'])}**  \n<span class='cook-hint'>{esc(h['cooked_at'][:10])}</span>",
                        unsafe_allow_html=True)


def account_section(display_name: str, test_mode: bool) -> None:
    st.markdown(f"**{md_safe(display_name)}**")
    if test_mode:
        st.caption("Test mode: no Google sign-in.")
    else:
        st.button("Sign out", on_click=st.logout, width="stretch")
    confirm = st.checkbox("I want to delete all my Cook AI data", key="confirm_delete")
    if st.button("Delete my data", disabled=not confirm, width="stretch"):
        db.delete_all_user_data(uid)
        st.session_state.clear()
        st.rerun()


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _init_database() -> bool:
    db.initialize_database()
    return True


uid, display_name, test_mode = get_user()
try:
    _init_database()
    data = db.load_all(uid)
except Exception as exc:  # network down, bad Turso secret...
    st.error("Cook AI cannot reach its database right now. Please try again in a minute.")
    st.caption(f"({type(exc).__name__})")
    st.stop()

if db.storage_mode() == "local" and not test_mode:
    st.warning("Cook AI is using a temporary local database. Add TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to keep users' data.")
if pending := st.session_state.pop("flash", None):
    st.toast(pending[0], icon=pending[1])

cart_count = len([i for i in data["shopping"] if not i["bought"]])
with st.container(key="topbar"):
    title_col, cart_col, account_col = st.columns([6, 1.2, 1])
    title_col.markdown("## 🍳 Cook AI")
    with cart_col.popover(f"🛒 {cart_count}" if cart_count else "🛒"):
        shopping_section(uid, data["shopping"])
    with account_col.popover("👤"):
        account_section(display_name, test_mode)

with st.container(key="bottomnav"):
    tab = pick("Navigation", list(NAV_TABS), key="nav", default=NAV_TABS[0])

if tab == NAV_TABS[0]:
    render_cook(uid, data)
elif tab == NAV_TABS[1]:
    render_kitchen(uid, data)
else:
    render_history(uid)

footer = "Cook AI · free test version"
if photos.photos_configured():
    footer += " · Photos from [Pexels](https://www.pexels.com)"
st.caption(footer)
