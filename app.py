import streamlit as st
import json as _json_hist
import pathlib as _pathlib_hist
import sqlite3 as _sqlite3
import os as _os

# ── Détection environnement : local vs Streamlit Cloud ──
# Sur Streamlit Cloud, STREAMLIT_SHARING_MODE ou IS_CLOUD est défini,
# ou simplement on vérifie si /mount/src existe (chemin typique Streamlit Cloud)
_IS_CLOUD = (
    _os.environ.get("STREAMLIT_SHARING_MODE") == "true" or
    _os.environ.get("IS_STREAMLIT_CLOUD") == "true" or
    _pathlib_hist.Path("/mount/src").exists()
)

# ── Persistance historique diagnostics ──
# Local  → SQLite (persistant entre sessions)
# Cloud  → session_state uniquement (session courante)
_DB_FILE = str(_pathlib_hist.Path(__file__).parent / "diagnostics.db")

def _init_db():
    if _IS_CLOUD: return
    try:
        with _sqlite3.connect(_DB_FILE) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS historique (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    date      TEXT,
                    heure     TEXT,
                    cadence   TEXT,
                    x1        TEXT,
                    source    TEXT,
                    critiques INTEGER,
                    warnings  INTEGER,
                    statut    TEXT,
                    mesures   TEXT,
                    alertes   TEXT
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS last_diag (
                    id      INTEGER PRIMARY KEY CHECK (id=1),
                    alertes TEXT,
                    mesures TEXT
                )""")
            con.commit()
    except Exception:
        pass

def _load_hist():
    """Charge l'historique — SQLite si local, session_state si cloud."""
    if _IS_CLOUD:
        return st.session_state.get("hist_diag", [])
    try:
        _init_db()
        with _sqlite3.connect(_DB_FILE) as con:
            rows = con.execute(
                "SELECT date,heure,cadence,x1,source,critiques,warnings,statut,mesures,alertes "
                "FROM historique ORDER BY id DESC LIMIT 100"
            ).fetchall()
        result = []
        for r in rows:
            entry = {
                "date": r[0], "heure": r[1], "cadence": r[2],
                "x1": r[3], "source": r[4],
                "critiques": r[5], "warnings": r[6], "statut": r[7],
            }
            try: entry["mesures"] = _json_hist.loads(r[8]) if r[8] else {}
            except Exception: entry["mesures"] = {}
            try: entry["alertes"] = _json_hist.loads(r[9]) if r[9] else []
            except Exception: entry["alertes"] = []
            result.append(entry)
        return result
    except Exception:
        return []

def _save_hist(hist):
    """Sauvegarde le 1er élément — SQLite si local, session_state si cloud."""
    if not hist: return
    if _IS_CLOUD:
        # Sur cloud : session_state suffit, déjà mis à jour avant l'appel
        return
    try:
        _init_db()
        h = hist[0]
        with _sqlite3.connect(_DB_FILE) as con:
            # Dédup : ne pas insérer si même date+heure+source que le dernier
            row = con.execute(
                "SELECT date,heure,source FROM historique ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and row[0] == h.get("date","") and row[1] == h.get("heure","") and row[2] == h.get("source",""):
                return
            con.execute(
                "INSERT INTO historique (date,heure,cadence,x1,source,critiques,warnings,statut,mesures,alertes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (h.get("date",""), h.get("heure",""), h.get("cadence",""),
                 h.get("x1",""), h.get("source",""),
                 h.get("critiques",0), h.get("warnings",0), h.get("statut",""),
                 _json_hist.dumps(h.get("mesures",{}), ensure_ascii=False),
                 _json_hist.dumps(h.get("alertes",[]),  ensure_ascii=False))
            )
            con.execute(
                "DELETE FROM historique WHERE id NOT IN "
                "(SELECT id FROM historique ORDER BY id DESC LIMIT 100)"
            )
            con.commit()
    except Exception:
        pass

def _clear_hist():
    if _IS_CLOUD:
        st.session_state["hist_diag"] = []
        return
    try:
        _init_db()
        with _sqlite3.connect(_DB_FILE) as con:
            con.execute("DELETE FROM historique")
            con.commit()
    except Exception:
        pass

_init_db()

def _save_last_diag(alertes, mesures):
    if _IS_CLOUD:
        return
    try:
        _init_db()
        with _sqlite3.connect(_DB_FILE) as con:
            con.execute("DELETE FROM last_diag")
            con.execute("INSERT INTO last_diag (id,alertes,mesures) VALUES (1,?,?)",
                (_json_hist.dumps(alertes, ensure_ascii=False),
                 _json_hist.dumps(mesures, ensure_ascii=False)))
            con.commit()
    except Exception: pass

def _load_last_diag():
    if _IS_CLOUD:
        return None, {}
    try:
        _init_db()
        with _sqlite3.connect(_DB_FILE) as con:
            row = con.execute("SELECT alertes,mesures FROM last_diag WHERE id=1").fetchone()
        if row:
            return (_json_hist.loads(row[0]) if row[0] else None,
                    _json_hist.loads(row[1]) if row[1] else {})
    except Exception: pass
    return None, {}

def _clear_last_diag():
    if _IS_CLOUD: return
    try:
        _init_db()
        with _sqlite3.connect(_DB_FILE) as con:
            con.execute("DELETE FROM last_diag")
            con.commit()
    except Exception: pass

def _dedup_insert(hist_list, new_entry):
    """Insert only if last entry has different heure (avoid duplicates on rerun)."""
    if hist_list and (
        hist_list[0].get("heure")  == new_entry.get("heure") and
        hist_list[0].get("date")   == new_entry.get("date")  and
        hist_list[0].get("source") == new_entry.get("source")
    ):
        return  # duplicate — skip
    hist_list.insert(0, new_entry)

import pandas as pd
import numpy as np
from scipy.optimize import minimize
import plotly.graph_objects as go

# ─────────────────────────────────────────────
#  CONFIGURATION PAGE  (doit être en 1er)
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="EMS — Centrale Thermique OCP",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────────
#  AUTHENTIFICATION
# ─────────────────────────────────────────────
# Chargé depuis st.secrets si disponible, sinon valeur par défaut (dev uniquement)
# Configurer dans .streamlit/secrets.toml:
#   [auth]
#   admin = "votre_mot_de_passe_securise"
def _get_users():
    try:
        _auth = st.secrets.get("auth", {})
        if _auth:
            return dict(_auth)
    except Exception:
        pass
    return {"admin": "centrale515"}

USERS = _get_users()

def login_page():
    st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Times+New+Roman&display=swap');
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stAppViewContainer"] {
            background: linear-gradient(160deg, #f0f7f0 0%, #e8f5e9 40%, #ffffff 100%);
            font-family: "Times New Roman", Times, serif;
        }
        .login-card {
            background: white;
            border-radius: 16px;
            padding: 2.5rem 2rem 2rem 2rem;
            box-shadow: 0 8px 32px rgba(0,120,0,0.13), 0 2px 8px rgba(0,0,0,0.07);
            border-top: 5px solid #1a7a1a;
            margin-top: 1rem;
        }
        .login-logo {
            display: flex;
            justify-content: center;
            margin-bottom: 0.7rem;
        }
        .login-logo img {
            width: 110px;
            height: 110px;
            object-fit: contain;
        }
        .login-title {
            font-family: "Times New Roman", Times, serif;
            color: #145214;
            font-size: 1.55rem;
            font-weight: 900;
            text-align: center;
            text-decoration: underline;
            text-underline-offset: 5px;
            margin: 0.2rem 0 0.1rem 0;
            line-height: 1.3;
            letter-spacing: 0.01em;
        }
        .login-sub {
            font-family: "Times New Roman", Times, serif;
            color: #2d7a2d;
            font-size: 1.05rem;
            font-weight: 700;
            text-align: center;
            text-decoration: underline;
            text-underline-offset: 4px;
            margin-bottom: 0.2rem;
        }

        .login-divider {
            border: none;
            border-top: 2px solid #c8e6c9;
            margin: 1rem 0 1.4rem 0;
        }
        /* Style les labels des inputs */
        .stTextInput label {
            font-family: "Times New Roman", Times, serif !important;
            font-size: 1rem !important;
            font-weight: 700 !important;
            color: #145214 !important;
        }
        .stTextInput input {
            font-family: "Times New Roman", Times, serif !important;
            border: 1.5px solid #a5d6a7 !important;
            border-radius: 8px !important;
        }
        .stTextInput input:focus {
            border-color: #1a7a1a !important;
            box-shadow: 0 0 0 2px rgba(26,122,26,0.15) !important;
        }
        div[data-testid="stFormSubmitButton"] button {
            background: linear-gradient(90deg, #1a7a1a, #2e9e2e) !important;
            color: white !important;
            font-family: "Times New Roman", Times, serif !important;
            font-size: 1.05rem !important;
            font-weight: 700 !important;
            border-radius: 8px !important;
            border: none !important;
            padding: 0.6rem 0 !important;
            margin-top: 0.5rem;
            letter-spacing: 0.03em;
        }
        div[data-testid="stFormSubmitButton"] button:hover {
            background: linear-gradient(90deg, #145214, #1a7a1a) !important;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(0,100,0,0.25) !important;
        }
        .login-footer {
            font-family: "Times New Roman", Times, serif;
            text-align: center;
            color: #aaa;
            font-size: 0.75rem;
            margin-top: 1.2rem;
            font-style: italic;
        }
    </style>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 1.4, 1])
    with col2:
        st.markdown(f'''
        <div class="login-card">
            <div class="login-logo">
                <img src="data:image/png;base64,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GWZUpnp1KnQlhYRKQioRUVwioAPTMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9k=" alt="OCP Logo"/>
            </div>
            <div class="login-title">Energy Management System<br>OCP Group</div>
            <div class="login-sub">Centrale Thermique 515A</div>
            <div style="font-family:Times New Roman,Times,serif;color:#555;font-size:0.9rem;font-style:italic;text-align:center;margin:0.2rem 0 0.8rem 0;">Unité 515A</div>
            <div style="border-top:2px solid #c8e6c9;margin:0 0 1.2rem 0;"></div>
        </div>
        ''', unsafe_allow_html=True)

        with st.form("login_form"):
            identifiant  = st.text_input("Identifiant",   placeholder="Votre identifiant")
            mot_de_passe = st.text_input("Mot de passe",  placeholder="Votre mot de passe", type="password")
            submitted = st.form_submit_button("🔐  Se connecter", use_container_width=True, type="primary")
            if submitted:
                if identifiant in USERS and USERS[identifiant] == mot_de_passe:
                    st.session_state["authenticated"] = True
                    st.session_state["username"]      = identifiant
                    st.rerun()
                else:
                    st.error("❌ Identifiant ou mot de passe incorrect")

        st.markdown('<div class="login-footer">© OCP Group — EMS Centrale Thermique | UNITE 515A</div>', unsafe_allow_html=True)

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    login_page()
    st.stop()


# ─────────────────────────────────────────────
#  CSS GLOBAL — THÈME OCP VERT
# ─────────────────────────────────────────────
st.markdown("""
<style>
    /* ── Fix file_uploader: 1 seul bouton Upload ── */
    [data-testid="stFileUploaderDropzone"] {
        background: transparent !important;
        border: none !important;
        padding: 0 !important;
        min-height: unset !important;
        box-shadow: none !important;
    }
    [data-testid="stFileUploaderDropzone"] small,
    [data-testid="stFileUploaderDropzone"] p,
    [data-testid="stFileUploaderDropzone"] svg { display:none !important; }
    /* Cacher le span "Upload" natif — garder seulement le bouton */
    [data-testid="stFileUploaderDropzone"] > div > span { display:none !important; }
    [data-testid="stFileUploaderDropzone"] > div > div > span { display:none !important; }
    /* Bouton: remplacer Browse files par Upload */
    [data-testid="stFileUploaderDropzone"] button span { display:none !important; }
    [data-testid="stFileUploaderDropzone"] button::after {
        content: "Upload" !important;
        font-family: "Times New Roman", Times, serif !important;
        font-size: 0.9rem !important;
        font-weight: 600 !important;
    }
    /* ── Fond app ── */
    [data-testid="stAppViewContainer"] {
        background: linear-gradient(160deg, #f0f7f0 0%, #f7faf7 60%, #ffffff 100%);
        font-family: "Times New Roman", Times, serif;
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #f0f7f0 0%, #e8f5e9 100%);
        border-right: 2px solid #c8e6c9;
        font-family: "Times New Roman", Times, serif;
    }
    /* ── Sidebar textes ── */
    [data-testid="stSidebar"] * {
        color: #145214 !important;
        font-family: "Times New Roman", Times, serif !important;
    }
    [data-testid="stSidebar"] .stButton button {
        background: linear-gradient(90deg, #1a7a1a, #2e9e2e) !important;
        border: none !important;
        color: white !important;
        font-family: "Times New Roman", Times, serif !important;
        font-weight: 700 !important;
        border-radius: 8px !important;
    }
    [data-testid="stSidebar"] .stButton button:hover {
        background: linear-gradient(90deg, #145214, #1a7a1a) !important;
    }
    [data-testid="stSidebar"] hr { border-color: #a5d6a7 !important; }
    [data-testid="stSidebar"] .stSlider [data-testid="stMarkdownContainer"] p { color: #2d7a2d !important; }
    /* ── En-tête principal ── */
    .header-wrapper {
        margin-bottom: 1.4rem;
    }
    /* Ligne 1: logo (hors card) + search à droite */
    .header-top-row {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        margin-bottom: 0.4rem;
        padding: 0 0.2rem;
    }
    .main-header-logo {
        width: 72px; height: 72px;
        object-fit: contain;
    }
    .header-search-box input {
        background: #f0f7f0;
        border: 1.5px solid #a5d6a7;
        border-radius: 22px;
        color: #145214;
        padding: 0.4rem 1.1rem;
        font-family: "Times New Roman", Times, serif;
        font-size: 0.9rem;
        width: 240px;
        outline: none;
    }
    .header-search-box input::placeholder { color: #81c784; font-style: italic; }
    /* Ligne 2: card titre */
    .main-header {
        background: white;
        padding: 1rem 1.8rem;
        border-radius: 12px;
        border-left: 6px solid #1a7a1a;
        border-top: 1px solid #c8e6c9;
        border-right: 1px solid #c8e6c9;
        border-bottom: 1px solid #c8e6c9;
        box-shadow: 0 3px 14px rgba(0,100,0,0.09);
    }
    .main-header h1 {
        font-family: "Times New Roman", Times, serif;
        color: #145214;
        font-size: 1.65rem;
        font-weight: 900;
        margin: 0 0 0.15rem 0;
        text-decoration: underline;
        text-underline-offset: 5px;
        letter-spacing: 0.01em;
    }
    .main-header p {
        font-family: "Times New Roman", Times, serif;
        color: #2d7a2d;
        font-size: 0.92rem;
        margin: 0;
        font-style: italic;
    }
    /* ── Titres de sections ── */
    .section-title {
        font-family: "Times New Roman", Times, serif;
        font-size: 1.05rem;
        font-weight: 700;
        color: #145214;
        border-bottom: 2px solid #81c784;
        padding-bottom: 0.3rem;
        margin-bottom: 1rem;
        font-style: italic;
    }
    /* ── Tabs ── */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        background: #e8f5e9;
        border-radius: 10px 10px 0 0;
        padding: 0.3rem 0.3rem 0;
        gap: 0.2rem;
        border-bottom: 2px solid #81c784;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        font-family: "Times New Roman", Times, serif !important;
        font-weight: 900 !important;
        font-size: 1rem !important;
        color: #2d7a2d !important;
        background: transparent !important;
        border-radius: 8px 8px 0 0 !important;
        padding: 0.55rem 1.3rem !important;
        border: none !important;
        letter-spacing: 0.01em;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] p {
        font-family: "Times New Roman", Times, serif !important;
        font-weight: 900 !important;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        background: white !important;
        color: #145214 !important;
        border-bottom: 3px solid #1a7a1a !important;
        box-shadow: 0 -2px 8px rgba(0,100,0,0.08) !important;
    }
    /* ── Métriques KPI ── */
    [data-testid="metric-container"] {
        background: white;
        border: 1px solid #c8e6c9;
        border-radius: 12px;
        padding: 1rem 1.2rem;
        box-shadow: 0 2px 8px rgba(0,100,0,0.07);
        border-top: 3px solid #2e9e2e;
    }
    [data-testid="metric-container"] label {
        font-family: "Times New Roman", Times, serif !important;
        font-size: 0.85rem !important;
        color: #555 !important;
        font-style: italic !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-family: "Times New Roman", Times, serif !important;
        font-size: 1.7rem !important;
        font-weight: 900 !important;
        color: #145214 !important;
    }
    /* ── Tag box diagnostic ── */
    .tag-box {
        font-family: monospace;
        font-size: 0.78rem;
        background: #e8f5e9;
        color: #145214;
        padding: 2px 7px;
        border-radius: 4px;
        border: 1px solid #a5d6a7;
        display: inline-block;
    }
    div[data-testid="stExpander"] {
        border: 1px solid #dde4ec;
        border-radius: 10px;
        border-left: 4px solid #4a6fa5;
        font-family: "Times New Roman", Times, serif !important;
        background: white;
        box-shadow: 0 1px 4px rgba(0,0,0,0.05);
        margin-bottom: 0.4rem;
    }
    div[data-testid="stExpander"] summary {
        font-family: "Times New Roman", Times, serif !important;
        font-weight: 700 !important;
        font-size: 0.93rem !important;
        color: #1a2e4a !important;
        padding: 0.55rem 1rem !important;
    }
    div[data-testid="stExpander"] summary:hover {
        background: #f4f7fb !important;
        border-radius: 8px;
    }
    /* Colorer ANOMALIE CRITIQUE en rouge dans le titre */
    div[data-testid="stExpander"] summary p {
        font-family: "Times New Roman", Times, serif !important;
        font-weight: 700 !important;
    }
    /* ── Textes généraux ── */
    p, label, div, span {
        font-family: "Times New Roman", Times, serif;
    }
    h1, h2, h3 {
        font-family: "Times New Roman", Times, serif !important;
        color: #145214 !important;
    }
    /* ── Inputs et selects ── */
    .stNumberInput input, .stTextInput input, .stSelectbox select {
        font-family: "Times New Roman", Times, serif !important;
        border-color: #a5d6a7 !important;
        border-radius: 8px !important;
    }
    /* ── Dividers ── */
    hr { border-color: #c8e6c9 !important; }
    /* ── Info/Success/Warning boxes ── */
    [data-testid="stAlert"] {
        border-radius: 8px !important;
        font-family: "Times New Roman", Times, serif !important;
    }
    /* ── Dataframes ── */
    [data-testid="stDataFrame"] {
        font-family: "Times New Roman", Times, serif !important;
    }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  PARAMÈTRES FIXES GE Nuovo Pignone
# ─────────────────────────────────────────────
P = {
    "P_HP": 57.0, "T_HP": 470.0, "h_HP": 3355.1, "Q_HP_max": 217.0,
    "eta_gen": 0.985,
    "P_collect_MP": 12.0, "T_collect_MP": 270.0, "h_MP": 2980.6,
    "P_collect_BP":  5.0, "T_collect_BP": 190.0, "h_BP": 2834.3,
    "h_exhaust": 2316.7, "P_cond": 0.081, "T_cond": 41.8,
    "h_condensat": 175.0,  # IAPWS-97 à 42°C
}

# ─────────────────────────────────────────────
#  TAGS PI — DIAGNOSTIC
# ─────────────────────────────────────────────
TAGS = {
    "P_HP_mes":          {"nom": "Pression vapeur HP",           "tag": "515APG10.PIC-328",  "nominal": 57,    "unite": "Bar",   "cat": "HP"},
    "T_HP_mes":          {"nom": "Température vapeur HP",        "tag": "515APG10.TIC-327",  "nominal": 470,   "unite": "°C",    "cat": "HP"},
    "Q_HP_mes":          {"nom": "Débit HP entrant (SAP)",       "tag": "515APG10.FI-203",   "nominal": 217,   "unite": "T/h",   "cat": "HP"},
    "P_elec_mes":        {"nom": "Puissance électrique",         "tag": "515APG10.JT-963A",  "nominal": 52.502,"unite": "MW",    "cat": "TRB"},
    "Q_HP_turb_mes":     {"nom": "Débit HP entrée turbine",      "tag": "515APG10.FI-151",   "nominal": 217,   "unite": "T/h",   "cat": "TRB"},
    "T_HP_turb_mes":     {"nom": "T° HP entrée turbine",         "tag": "515APG10.TI-154",   "nominal": 470,   "unite": "°C",    "cat": "TRB"},
    "P_HP_turb_mes":     {"nom": "P vapeur HP entrée turbine",   "tag": "515APG10.PI-153",   "nominal": 57,    "unite": "Bar",   "cat": "TRB"},
    "N_turb_mes":        {"nom": "Vitesse rotation turbine",     "tag": "515APG10.SE-288",   "nominal": 3000,  "unite": "RPM",   "cat": "TRB"},
    "Q_sout_MP_mes":     {"nom": "Débit soutirage MP",           "tag": "515APG10.FI-541",   "nominal": 12,    "unite": "T/h",   "cat": "MP"},
    "Q_sout_LP_mes":     {"nom": "Débit soutirage LP",           "tag": "515APG10.FI-544",   "nominal": 57.4,  "unite": "T/h",   "cat": "BP"},
    "Q_det_MP_mes":      {"nom": "Débit bypass HP→MP",           "tag": "515APG10.FI-005",   "nominal": 0,     "unite": "T/h",   "cat": "BYPASS"},
    "P_det_MP_mes":      {"nom": "Pression détente bypass MP",   "tag": "515APG10.PIC-004",  "nominal": 12,    "unite": "Bar",   "cat": "BYPASS"},
    "T_det_MP_mes":      {"nom": "T° détente bypass MP",         "tag": "515APG10.TIC-003",  "nominal": 270,   "unite": "°C",    "cat": "BYPASS"},
    "P_det_BP_mes":      {"nom": "Pression détente HP→BP",       "tag": "515APG10.PI-032",   "nominal": 5,     "unite": "Bar",   "cat": "BYPASS"},
    "T_det_BP_mes":      {"nom": "T° détente bypass BP",         "tag": "515APG10.TIC-023",  "nominal": 190,   "unite": "°C",    "cat": "BYPASS"},
    "P_det_MP_BP_mes":   {"nom": "Pression détente MP→BP",       "tag": "515APG10.PIC-044",  "nominal": 5,     "unite": "Bar",   "cat": "BYPASS"},
    "P_cond_mes":        {"nom": "Pression condenseur",          "tag": "515APG10.PI-252",   "nominal": 0.081, "unite": "Bar a", "cat": "COND"},
    "T_cond_mes":        {"nom": "Température condenseur",       "tag": "515APG10.TI-170",   "nominal": 41.8,  "unite": "°C",    "cat": "COND"},
    "pct_PV004_mes":     {"nom": "Ouverture PV-004 (bypass MP)", "tag": "515APG10.PV-004",   "nominal": 0,     "unite": "%",     "cat": "VANNE"},
    "pct_PV548_mes":     {"nom": "Ouverture PV-548",             "tag": "515APG10.PV-548",   "nominal": None,  "unite": "%",     "cat": "VANNE"},
    "pct_PV024_mes":     {"nom": "Ouverture PV-024 (bypass BP)", "tag": "515APG10.PV-024",   "nominal": 0,     "unite": "%",     "cat": "VANNE"},
    "pct_PV551B_mes":    {"nom": "Ouverture PV-551B",            "tag": "515APG10.PV-551B",  "nominal": None,  "unite": "%",     "cat": "VANNE"},
    "pct_PV044_mes":     {"nom": "Ouverture PV044 (MP→BP)",      "tag": "515APG10.PV044",    "nominal": 0,     "unite": "%",     "cat": "VANNE"},
    "pct_TV003_mes":     {"nom": "Arrosage TV-003 bypass MP",    "tag": "515APG10.TV-003",   "nominal": None,  "unite": "%",     "cat": "ARROS"},
    "pct_TV083_mes":     {"nom": "Arrosage TV-083 soutirée MP",  "tag": "515APG10.TV-083",   "nominal": None,  "unite": "%",     "cat": "ARROS"},
    "pct_TV023_mes":     {"nom": "Arrosage TV-023 bypass BP",    "tag": "515APG10.TV-023",   "nominal": None,  "unite": "%",     "cat": "ARROS"},
    "Q_arros_bypass_MP": {"nom": "Débit arrosage bypass MP",     "tag": "515APG10.FI-001",   "nominal": None,  "unite": "m³/h",  "cat": "ARROS"},
    "Q_arros_bypass_BP": {"nom": "Débit arrosage bypass BP",     "tag": "515APG10.FI021",    "nominal": None,  "unite": "m³/h",  "cat": "ARROS"},
    "Q_arros_MP_sout":   {"nom": "Débit arrosage MP soutirée",   "tag": "515APG10.FI-081",   "nominal": None,  "unite": "m³/h",  "cat": "ARROS"},
    "Q_DAP_MP_mes":      {"nom": "Débit DAP MP",                 "tag": "515APG10.FI-066",   "nominal": 3,     "unite": "T/h",   "cat": "CONSO"},
    "P_DAP_MP_mes":      {"nom": "Pression DAP MP",              "tag": "515APG10.PI-065",   "nominal": 12,    "unite": "Bar",   "cat": "CONSO"},
    "T_DAP_MP_mes":      {"nom": "Température DAP MP",           "tag": "515APG10.TIC-063",  "nominal": 270,   "unite": "°C",    "cat": "CONSO"},
    "Q_JPH_recep_mes":   {"nom": "Débit réception JPH MP",       "tag": "515APG10.FI-043A",  "nominal": 0,     "unite": "T/h",   "cat": "CONSO"},
    "Q_JPH_transf_mes":  {"nom": "Débit transfert JPH MP",       "tag": "515APG10.FI-043B",  "nominal": 0,     "unite": "T/h",   "cat": "CONSO"},
    "P_JPH_MP_mes":      {"nom": "Pression JPH MP",              "tag": "515APG10.PI-041",   "nominal": 12,    "unite": "Bar",   "cat": "CONSO"},
    "Q_CAP_BP_mes":      {"nom": "Débit vapeur BP CAP",          "tag": "515APG10.FI-104",   "nominal": 0,     "unite": "T/h",   "cat": "CONSO"},
    "P_CAP_BP_mes":      {"nom": "Pression vapeur BP CAP",       "tag": "515APG10.PI-106",   "nominal": 5,     "unite": "Bar",   "cat": "CONSO"},
    "T_CAP_BP_mes":      {"nom": "Température vapeur BP CAP",    "tag": "515APG10.TIC-103",  "nominal": 190,   "unite": "°C",    "cat": "CONSO"},
    "pct_ATM1_mes":      {"nom": "Vanne sécurité ATM 1",         "tag": "515APG10.PIC-128A", "nominal": 0,     "unite": "%",     "cat": "ATM"},
    "pct_ATM2_mes":      {"nom": "Vanne sécurité ATM 2",         "tag": "515APG10.PIC-128B", "nominal": 0,     "unite": "%",     "cat": "ATM"},
    "Q_HRS_mes":         {"nom": "Débit BP entrant HRS",         "tag": "515APG10.FI370",    "nominal": None,  "unite": "T/h",   "cat": "CONSO"},
}

DIAG = {
    "P_HP_mes":       {"cause": "Chute pression HP amont — fuite ligne ou problème SAP",              "sol": "1) Inspection tuyauterie HP  2) Check vanne HV-007  3) Coordination unité sulfurique  4) Recalibrer PIC-328"},
    "T_HP_mes":       {"cause": "Désurchauffe excessive ou anomalie chaudière amont",                  "sol": "1) Contrôler désurchauffeur TV-003/TV-023  2) Vérifier chaudière amont  3) Recalibrer TIC-327"},
    "Q_HP_mes":       {"cause": "Cadence SAP réduite ou fuite collecteur HP",                          "sol": "1) Vérifier cadence SAP (objectif 100%)  2) Inspection collecteur HP  3) Recalibrer FI-203"},
    "P_elec_mes":     {"cause": "Dégradation interne turbine, rendement génératrice réduit, ou débit x1 insuffisant (vapeur en expansion trop faible)", "sol": "1) Vérifier débit turbine x1 (FI-151) — cause principale si x1 < nominal  2) Inspection turbine GE  3) Mesurer η génératrice  4) Réduire bypasses  5) Planifier maintenance préventive"},
    "Q_HP_turb_mes":  {"cause": "Vanne HV-012 défaillante ou débitmètre FI-151 faussé",               "sol": "1) Vérification vanne HV-012  2) Recalibrer FI-151  3) Comparer avec FI-203"},
    "T_HP_turb_mes":  {"cause": "Perte thermique avant turbine ou thermocouple TI-154 défaillant",     "sol": "1) Vérifier isolation ligne HP  2) Recalibrer TI-154  3) Comparer TIC-327 vs TI-154"},
    "P_HP_turb_mes":  {"cause": "Chute pression localisée avant turbine ou capteur mal étalonné",      "sol": "1) Inspecter filtre admission turbine  2) Recalibrer PI-153  3) Comparer PIC-328 vs PI-153"},
    "N_turb_mes":     {"cause": "Régulateur vitesse GE défaillant ou surcharge réseau",                "sol": "1) Vérifier régulateur vitesse GE (setpoint 3000 RPM)  2) Contrôler charge réseau  3) Recalibrer SE-288"},
    "Q_sout_MP_mes":  {"cause": "Vanne PV-004 mal réglée, demande client MP anormale ou débit turbine x1 insuffisant", "sol": "1) Vérifier débit turbine x1 (FI-151)  2) Recalibrer vanne PV-004  3) Vérifier demande clients MP  4) Recalibrer FI-541"},
    "Q_sout_LP_mes":  {"cause": "Vanne PV-024 mal réglée, demande clients BP anormale ou débit turbine x1 insuffisant", "sol": "1) Vérifier débit turbine x1 (FI-151)  2) Recalibrer vanne PV-024  3) Vérifier demande clients BP  4) Recalibrer FI-544"},
    "Q_det_MP_mes":   {"cause": "Bypass HP→MP ouvert inutilement — PERTE ÉNERGÉTIQUE DIRECTE",        "sol": "1) Vérifier si clients MP satisfaits par soutirage  2) Fermer PV-004  3) Inspecter fuite interne"},
    "P_det_MP_mes":   {"cause": "Régulateur PIC-004 mal étalonné — pression non régulée à 12 Bar",    "sol": "1) Recalibrer PIC-004  2) Vérifier consigne (cible 12 Bar)  3) Inspecter vanne PV-004"},
    "T_det_MP_mes":   {"cause": "Désurchauffe TV-003 insuffisante — T° sortie trop haute",             "sol": "1) Augmenter ouverture TV-003  2) Cible T° = 270°C  3) Recalibrer TIC-003  4) Vérifier FI-001"},
    "P_det_BP_mes":   {"cause": "Capteur PI-032 mal étalonné ou détente n'atteint pas 5 Bar",          "sol": "1) Recalibrer PI-032  2) Vérifier consigne bypass BP (cible 5 Bar)  3) Inspecter PV-024"},
    "T_det_BP_mes":   {"cause": "Désurchauffe TV-023 insuffisante — T° trop haute pour réseau BP",     "sol": "1) Augmenter ouverture TV-023  2) Cible T° = 190°C  3) Recalibrer TIC-023  4) Vérifier FI021"},
    "P_cond_mes":     {"cause": "Perte de vide condenseur : fuite air, pompes vide défaillantes",      "sol": "1) Test étanchéité condenseur  2) Contrôler pompes à vide  3) Purger gaz incondensables  4) Recalibrer PI-252"},
    "T_cond_mes":     {"cause": "Eau refroidissement trop chaude ou encrassement faisceaux",            "sol": "1) Vérifier circuit eau refroidissement  2) Curage chimique faisceaux  3) Recalibrer TI-170"},
    "pct_PV004_mes":  {"cause": "Bypass MP ouvert inutilement — vapeur HP détendue sans travail = PERTE", "sol": "1) Fermer PV-004 si clients MP satisfaits  2) Objectif = 0%  3) Vérifier fuite interne"},
    "pct_PV024_mes":  {"cause": "Bypass BP ouvert inutilement — vapeur HP détendue sans travail = PERTE", "sol": "1) Fermer PV-024 si clients BP satisfaits  2) Objectif = 0%  3) Vérifier fuite interne"},
    "pct_ATM1_mes":   {"cause": "Surpression réseau vapeur ou ouverture intempestive vanne ATM — perte vapeur directe vers atmosphère", "sol": "1) Identifier source surpression  2) Vérifier pression collecteurs HP/MP/BP  3) Intervention maintenance urgente  4) Recalibrer PIC-128A"},
    "pct_ATM2_mes":   {"cause": "Surpression réseau vapeur ou ouverture intempestive vanne ATM — perte vapeur directe vers atmosphère", "sol": "1) Identifier source surpression  2) Vérifier pression collecteurs HP/MP/BP  3) Intervention maintenance urgente  4) Recalibrer PIC-128B"},
    "Q_DAP_MP_mes":   {"cause": "Consommation DAP MP anormale ou vanne alimentation mal réglée",       "sol": "1) Vérifier état DAP MP  2) Recalibrer vanne alimentation  3) Recalibrer FI-066"},
    "P_DAP_MP_mes":   {"cause": "Pression MP au DAP anormale — chute réseau ou régulateur défaillant", "sol": "1) Vérifier pression collecteur MP (12 Bar)  2) Inspecter régulateur DAP  3) Recalibrer PI-065"},
    "T_DAP_MP_mes":   {"cause": "Température MP au DAP hors gamme",                                    "sol": "1) Vérifier T° collecteur MP (cible 270°C)  2) Ajuster désurchauffe  3) Recalibrer TIC-063"},
}

# ─────────────────────────────────────────────
#  HELPER FUNCTIONS — UI
# ─────────────────────────────────────────────
def bar_color(pct):
    """Couleur barre selon % (plus c'est haut, mieux c'est)."""
    if pct >= 70: return "#1a7a1a"
    if pct >= 40: return "#f9a825"
    return "#c62828"

def bar_color_inv(pct):
    """Couleur barre selon % (plus c'est bas, mieux c'est)."""
    if pct <= 30: return "#1a7a1a"
    if pct <= 50: return "#f9a825"
    return "#c62828"


def calc_puissance(x1, x4, x5):
    """Puissance électrique (MW) — Formule constructeur GE"""
    if x1 <= 0:
        return 0.0
    return max(0.0, ((x1 - x4 - x5) / 3.5) + (x4 / 8) + (x5 / 6.5))

def calc_energie_entree(x1, x2, x3):
    """Énergie enthalpique entrante HP (MW)"""
    return (x1 + x2 + x3) * P["h_HP"] / 3600  # T/h × kJ/kg ÷ 3600 → MW

def calc_efficacite(x1, x2, x3, x4, x5):
    """Efficacité énergétique globale (%)"""
    E_MW = calc_energie_entree(x1, x2, x3)
    if E_MW <= 0:
        return 0.0
    return (calc_puissance(x1, x4, x5) / E_MW) * 100.0

def bilan_complet(cadence, x1, x2, x3, x4, x5, D_MP, D_BP, Q_JPH_recep, Q_HRS):
    Q_HP_dispo = round((cadence / 100.0) * P["Q_HP_max"], 2)
    x6         = max(0.0, x1 - x4 - x5)
    alim_MP    = x2 + x4 + Q_JPH_recep
    alim_BP    = x3 + x5 + Q_HRS
    return {
        "Q_HP_dispo":  Q_HP_dispo,
        "x6":          x6,
        "P_elec_MW":   calc_puissance(x1, x4, x5),
        "E_entree_MW": calc_energie_entree(x1, x2, x3),
        "eta_glob":    calc_efficacite(x1, x2, x3, x4, x5),
        "bilan_HP":    x1 + x2 + x3 - Q_HP_dispo,
        "bilan_turb":  x1 - x4 - x5 - x6,
        "alim_MP":     alim_MP,
        "alim_BP":     alim_BP,
        "ecart_MP":    alim_MP - D_MP,
        "ecart_BP":    alim_BP - D_BP,
    }

# ─────────────────────────────────────────────
#  OPTIMISEUR SLSQP
# ─────────────────────────────────────────────
def optimiser(cadence, D_MP, D_BP, Q_JPH_recep, Q_HRS):
    Q_HP = (cadence / 100.0) * P["Q_HP_max"]
    def obj(x):
        return -calc_efficacite(x[0], x[1], x[2], x[3], x[4])
    contraintes = [
        {"type": "eq",   "fun": lambda x: x[0] + x[1] + x[2] - Q_HP},
        {"type": "ineq", "fun": lambda x: x[1] + x[3] + Q_JPH_recep - D_MP},
        {"type": "ineq", "fun": lambda x: x[2] + x[4] + Q_HRS - D_BP},
        {"type": "ineq", "fun": lambda x: x[0] - x[3] - x[4]},
    ]
    bornes = [(100, Q_HP), (0, Q_HP), (0, Q_HP), (0, 12), (0, 57)]
    x0 = [min(217, Q_HP * 0.9), 0, 0, 12, 57.4]
    try:
        res = minimize(obj, x0, method="SLSQP", bounds=bornes,
                       constraints=contraintes, options={"ftol": 1e-8, "maxiter": 1000})
        if res.success:
            x1, x2, x3, x4, x5 = res.x
            return {
                "success": True,
                "x1": round(x1, 2), "x2": round(x2, 2), "x3": round(x3, 2),
                "x4": round(x4, 2), "x5": round(x5, 2),
                "x6": round(max(0, x1 - x4 - x5), 2),
                "P_elec_MW": round(calc_puissance(x1, x4, x5), 3),
                "eta_opt":   round(calc_efficacite(x1, x2, x3, x4, x5), 2),
            }
    except Exception as _e:  # noqa: BLE001 — scipy errors non critiques
        pass  # optimizer failed — return {"success": False}
    return {"success": False}

# ─────────────────────────────────────────────
#  DIAGNOSTIC
# ─────────────────────────────────────────────
def diagnostiquer(mesures):
    alertes = []
    # Recalculer x3 = Q_HP - x1 - x2
    _x1 = mesures.get("x1", mesures.get("Q_HP_turb_mes", 217.0)) or 217.0
    _x2 = mesures.get("x2", mesures.get("Q_det_MP_mes",    0.0)) or 0.0
    _cadence = mesures.get("cadence", 100.0) or 100.0
    _Q_HP = round((_cadence / 100.0) * P["Q_HP_max"], 2)
    _x3 = max(0.0, _Q_HP - _x1 - _x2)
    mesures = dict(mesures)
    mesures["x3"] = _x3
    for code, val in mesures.items():
        if val is None or code not in TAGS:
            continue
        info    = TAGS[code]
        nominal = info["nominal"]
        if nominal is None:
            continue

        # ── Cas spécial 1 : bypass (nominal=0) ──────────────────
        # nominal=0 → logique spéciale selon le code
        if nominal == 0:
            if code in ("pct_ATM1_mes", "pct_ATM2_mes"):
                # ATM: sécurité vapeur — max physique 80 T/h
                if val <= 0:
                    statut = "✅ OK"
                    ecart  = 0.0
                elif val <= 20:
                    statut = "⚠️ WARNING"
                    ecart  = round(val / 80 * 100, 1)
                else:
                    statut = "🔴 CRITIQUE"
                    ecart  = round(val / 80 * 100, 1)
            elif code in ("Q_det_MP_mes", "pct_PV004_mes",
                          "Q_det_BP_mes", "pct_PV024_mes",
                          "pct_PV044_mes"):
                # Bypasses: nominal=0 → tout écart = perte directe
                if val > 0:
                    statut = "🔴 CRITIQUE"
                    ecart  = 100.0
                else:
                    statut = "✅ OK"
                    ecart  = 0.0
            else:
                continue  # autres nominal=0 → skip

        # ── Cas spécial 2 : soutirage MP (x4) ───────────────────
        # x1 < 200 T/h → x4 DOIT être 0 → sinon CRITIQUE
        # x1 >= 200 T/h → x4 ≤ 12 T/h max → sinon CRITIQUE
        elif code == "Q_sout_MP_mes":
            # Utiliser x1 (clé harmonisée) avec fallback sur nominal
            x1_courant = float(mesures.get("x1", mesures.get("x1_turbine", 217.0)) or 217.0)
            if x1_courant < 200:
                if val > 0:
                    statut = "🔴 CRITIQUE"
                    ecart  = 100.0
                else:
                    # x4=0 avec x1<200 = condition normale → skip
                    continue
            else:
                # x1 >= 200 → x4 max = 12 T/h
                if val > 12:
                    statut = "🔴 CRITIQUE"
                    ecart  = round((val - 12) / 12 * 100, 2)
                elif val > 12 * 0.95:
                    statut = "✅ OK"
                    ecart  = round((val - 12) / 12 * 100, 2)
                else:
                    ecart  = round((val - nominal) / abs(nominal) * 100, 2)
                    if abs(ecart) >= 10:
                        statut = "🔴 CRITIQUE"
                    elif abs(ecart) >= 5:
                        statut = "⚠️ WARNING"
                    else:
                        statut = "✅ OK"

        # ── Cas normal : écart % vs nominal ─────────────────────
        else:
            ecart = (val - nominal) / abs(nominal) * 100.0
            if abs(ecart) >= 10:
                statut = "🔴 CRITIQUE"
            elif abs(ecart) >= 5:
                statut = "⚠️ WARNING"
            else:
                statut = "✅ OK"

        if statut != "✅ OK":
            # Cause/sol dynamique selon x1 pour les soutirages
            if code == "P_elec_mes":
                x1_c = float(mesures.get("x1", mesures.get("x1_turbine", 217.0)) or 217.0)
                nom_x1 = 217.0
                if x1_c < nom_x1 * 0.9:  # x1 < 90% nominal
                    cause = (f"Débit turbine x1 insuffisant ({x1_c:.1f} T/h) — "
                             f"puissance électrique directement liée à x1. "
                             f"Vérifier avant toute autre intervention.")
                    sol   = ("1) Vérifier débit HP entrant x1 (FI-151)  "
                             "2) Contrôler cadence SAP (FI-203)  "
                             "3) Si x1 normal: inspection turbine GE  "
                             "4) Mesurer rendement génératrice  "
                             "5) Réduire bypasses x2/x3 si ouverts")
                else:
                    cause = DIAG.get(code, {}).get("cause", "—")
                    sol   = DIAG.get(code, {}).get("sol",   "—")
            elif code == "Q_sout_MP_mes":
                x1_c = float(mesures.get("x1", mesures.get("x1_turbine", 217.0)) or 217.0)
                if x1_c < 200:
                    cause = (f"Débit turbine x1 insuffisant ({x1_c:.1f} T/h < 200 T/h) — "
                             f"soutirage MP ne peut pas être satisfait à pleine capacité")
                    sol   = ("1) Vérifier débit HP entrant (FI-203 / Q_HP)  "
                             "2) Contrôler cadence SAP  "
                             "3) Vérifier vanne admission turbine HV-012  "
                             "4) Recalibrer FI-151 si nécessaire")
                else:
                    cause = DIAG.get(code, {}).get("cause", "—")
                    sol   = DIAG.get(code, {}).get("sol",   "—")
            elif code == "Q_sout_LP_mes":
                x1_c = float(mesures.get("x1", mesures.get("x1_turbine", 217.0)) or 217.0)
                if x1_c < 100:
                    cause = (f"Débit turbine x1 insuffisant ({x1_c:.1f} T/h < 100 T/h) — "
                             f"soutirage BP ne peut pas être satisfait à pleine capacité")
                    sol   = ("1) Vérifier débit HP entrant (FI-203 / Q_HP)  "
                             "2) Contrôler cadence SAP  "
                             "3) Vérifier vanne admission turbine HV-012  "
                             "4) Recalibrer FI-151 si nécessaire")
                else:
                    cause = DIAG.get(code, {}).get("cause", "—")
                    sol   = DIAG.get(code, {}).get("sol",   "—")
            else:
                cause = DIAG.get(code, {}).get("cause", "—")
                sol   = DIAG.get(code, {}).get("sol",   "—")
        else:
            cause = ""
            sol   = ""

        alertes.append({
            "code": code, "nom": info["nom"], "tag": info["tag"],
            "nominal": nominal, "mesure": val,
            "ecart": round(ecart, 2), "statut": statut,
            "unite": info["unite"], "cause": cause, "sol": sol,
        })
    return sorted(alertes, key=lambda a: abs(a["ecart"]), reverse=True)

# ─────────────────────────────────────────────
#  EN-TÊTE
# ─────────────────────────────────────────────
st.markdown("""
<div class="header-wrapper">
  <div class="header-top-row">
    <img class="main-header-logo" src="data:image/png;base64,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GWZUpnp1KnQlhYRKQioRUVwioAPTMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9k=" alt="OCP"/>
    <div class="header-search-box">
      <input type="text" id="ems-search" placeholder="&#128269; Onglet..." autocomplete="off"
        style="font-family:Times New Roman,Times,serif;"
        onkeydown="if(event.key==='Escape'){this.value='';}"
        oninput="(function(){
          var inp=document.getElementById('ems-search');
          var v=inp.value.trim().toLowerCase();
          if(v.length<2)return;
          function tryClick(){
            var tabs=Array.from(document.querySelectorAll(
              'button[role=tab],[data-baseweb=tab],.stTabs button,[data-testid=stTab]'
            ));
            if(!tabs.length)return false;
            var best=null,score=999;
            for(var i=0;i<tabs.length;i++){
              var t=tabs[i].innerText.trim().toLowerCase();
              var idx=t.indexOf(v);
              if(idx>=0&&idx<score){best=tabs[i];score=idx;}
            }
            if(best){best.click();inp.value='';inp.blur();return true;}
            return false;
          }
          if(!tryClick()){
            var n=0;
            var id=setInterval(function(){
              if(tryClick()||++n>15)clearInterval(id);
            },100);
          }
        })()" />
    </div>
  </div>
  <div class="main-header">
    <h1>Energy Management System &mdash; OCP Group</h1>
    <p>Unit&eacute; 515A &nbsp;&bull;&nbsp; Centrale Thermique &nbsp;&bull;&nbsp; R&eacute;seau vapeur HP / MP / BP</p>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    # Bouton déconnexion
    username = st.session_state.get("username", "?")
    st.markdown(f"👤 Connecté : **{username}**")
    if st.button("🚪 Déconnexion", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()
    st.divider()
    st.header("📁 Source des données")

    source = st.radio("Mode", ["📊 Excel", "🔴 PI Server"], horizontal=True)

    # ── Valeurs par défaut ──
    v = {
        "cadence": 100.0, "x1": 217.0, "x2": 0.0, "x3": 0.0, "x4": 12.0, "x5": 57.4,
        "Q_DAP_MP": 3.0, "Q_ejecteur": 1.0, "Q_JPH_transf": 80.0, "Q_JPH_recep": 80.0,
        "Q_BP_J": 40.0, "Q_BP_K": 40.0, "Q_BP_L": 40.0, "Q_DAP_BP": 8.0, "Q_HRS": 72.0,
        # tags diagnostic
        "N_turb": 3000.0, "P_elec_mes": 52.502, "P_HP": 57.0, "T_HP": 470.0,
        "P_cond": 0.081, "T_cond": 41.8, "pct_PV004": 0.0, "pct_PV024": 0.0,
        "pct_ATM1": 0.0, "pct_ATM2": 0.0,
    }

    # ══════════════════════════════════════════
    # MODE EXCEL — SOUTENANCE
    # ══════════════════════════════════════════
    if source == "📊 Excel":
        st.markdown('<p style="font-family:Times New Roman,Times,serif;font-size:0.85rem;font-weight:700;color:#1a3a6e;margin-bottom:0.2rem;">📂 Template_Données_Réelles.xlsx</p>', unsafe_allow_html=True)
        fichier = st.file_uploader(
            "x",
            type=["xlsx"],
            label_visibility="hidden",
            key="excel_uploader"
        )


        if fichier:
            try:
                df_xl = pd.read_excel(fichier, sheet_name="Données_Réelles")
                # Lire colonne Paramètre → Valeur
                data_xl = dict(zip(df_xl["Paramètre"], df_xl["Valeur"]))

                def gv(key, default):
                    val = data_xl.get(key, default)
                    return float(val) if val is not None else default

                v["cadence"]      = gv("cadence_SAP",  100.0)
                v["x1"]           = gv("x1_turbine",   217.0)
                v["x2"]           = gv("x2_bypass_MP",   0.0)
                v["x3"]           = gv("x3_bypass_BP",   0.0)
                v["x4"]           = gv("x4_sout_MP",    12.0)
                v["x5"]           = gv("x5_sout_LP",    57.4)
                v["Q_DAP_MP"]     = gv("Q_DAP_MP",       3.0)
                v["Q_ejecteur"]   = gv("Q_ejecteur",     1.0)
                v["Q_JPH_transf"] = gv("Q_JPH_transf", 80.00)
                v["Q_JPH_recep"]  = gv("Q_JPH_recep",    0.0)
                v["Q_BP_J"]       = gv("Q_BP_J",        40.0)
                v["Q_BP_K"]       = gv("Q_BP_K",        40.0)
                v["Q_BP_L"]       = gv("Q_BP_L",        40.0)
                v["Q_DAP_BP"]     = gv("Q_DAP_BP",       8.0)
                v["Q_HRS"]        = gv("Q_HRS",         72.0)
                v["N_turb"]       = gv("N_turb",       3000.0)
                v["P_elec_mes"]   = gv("P_elec",        52.502)
                v["P_HP"]         = gv("P_HP",          57.0)
                v["T_HP"]         = gv("T_HP",         470.0)
                v["P_cond"]       = gv("P_cond",       0.081)
                v["T_cond"]       = gv("T_cond",        41.8)
                v["pct_PV004"]    = gv("pct_PV004",      0.0)
                v["pct_PV024"]    = gv("pct_PV024",      0.0)
                v["pct_ATM1"]     = gv("pct_ATM1",       0.0)
                v["pct_ATM2"]     = gv("pct_ATM2",       0.0)

                st.success(f"✅ {len(data_xl)} paramètres chargés automatiquement")
                st.session_state["excel_data"] = v
                # Réinitialiser résultats diagnostic précédents
                st.session_state.pop("alertes_diag", None)
                st.session_state.pop("mesures_snapshot", None)
                _clear_last_diag()
            except Exception as e:
                st.error(f"Erreur lecture Excel: {e}")

        elif "excel_data" in st.session_state:
            v = st.session_state["excel_data"]
            st.info("📊 Données Excel précédentes utilisées")

    # ══════════════════════════════════════════
    # MODE PI SERVER — ENTREPRISE
    # ══════════════════════════════════════════
    else:
        # ── Configuration PI Server — chargée depuis st.secrets ou variables d'env ──
        # Pour configurer: créez .streamlit/secrets.toml avec:
        #   [pi]
        #   base = "http://PI_SERVER_IP/piwebapi"
        #   user = "votre_user"
        #   pass = "votre_password"
        _pi_secrets = st.secrets.get("pi", {}) if hasattr(st, "secrets") else {}
        PI_BASE = _pi_secrets.get("base", _os.environ.get("PI_BASE", "http://PI_SERVER_IP/piwebapi"))
        PI_USER = _pi_secrets.get("user", _os.environ.get("PI_USER", "votre_user"))
        PI_PASS = _pi_secrets.get("pass", _os.environ.get("PI_PASS", "votre_password"))

        PI_TAGS = {
            "x1_turbine":   "515APG10.FI-151",
            "x4_sout_MP":   "515APG10.FI-541",
            "x5_sout_LP":   "515APG10.FI-104",
            "x2_bypass_MP": "515APG10.FI-005",
            "cadence_SAP":  "515APG10.FI-203",
            "P_HP":         "515APG10.PIC-328",
            "T_HP":         "515APG10.TIC-327",
            "P_elec":       "515APG10.JT-963A",
            "P_cond":       "515APG10.PI-252",
            "T_cond":       "515APG10.TI-170",
            "pct_PV004":    "515APG10.PV-004",
            "pct_PV024":    "515APG10.PV-024",
            "pct_ATM1":     "515APG10.PIC-128A",
            "pct_ATM2":     "515APG10.PIC-128B",
        }

        def get_pi_value(tag):
            try:
                import requests
                url = f"{PI_BASE}/streams/{tag}/value"
                r = requests.get(url, auth=(PI_USER, PI_PASS),
                                 verify=False, timeout=5)
                r.raise_for_status()
                return float(r.json()["Value"])
            except (KeyError, TypeError, ValueError):
                return None  # tag returned but value not parseable
            except Exception:
                return None  # connection / auth error

        col_pi1, col_pi2 = st.columns([2, 1])
        with col_pi1:
            st.info(f"🔴 Serveur PI : `{PI_BASE}`")
        with col_pi2:
            refresh = st.button("🔄 Rafraîchir", use_container_width=True, type="primary")

        if refresh:
            with st.spinner("Connexion PI Server..."):
                pi_vals = {}
                errors  = []
                for key, tag in PI_TAGS.items():
                    val = get_pi_value(tag)
                    if val is not None:
                        pi_vals[key] = val
                    else:
                        errors.append(tag)

            if errors:
                st.error(f"❌ Tags non récupérés : {', '.join(errors)}")
            if pi_vals:
                # Mapper vers v
                mapping = {
                    "x1_turbine":   ("x1",        217.0),
                    "x4_sout_MP":   ("x4",         12.0),
                    "x5_sout_LP":   ("x5",         57.4),
                    "x2_bypass_MP": ("x2",          0.0),
                    "cadence_SAP":  ("cadence",   100.0),
                    "P_HP":         ("P_HP",       57.0),
                    "T_HP":         ("T_HP",      470.0),
                    "P_elec":       ("P_elec_mes", 52.502),
                    "P_cond":       ("P_cond",    0.081),
                    "T_cond":       ("T_cond",     41.8),
                    "pct_PV004":    ("pct_PV004",   0.0),
                    "pct_PV024":    ("pct_PV024",   0.0),
                    "pct_ATM1":     ("pct_ATM1",    0.0),
                    "pct_ATM2":     ("pct_ATM2",    0.0),
                }
                for pi_key, (v_key, default) in mapping.items():
                    v[v_key] = pi_vals.get(pi_key, default)
                st.session_state["pi_data"] = v
                # Réinitialiser résultats diagnostic précédents
                st.session_state.pop("alertes_diag", None)
                st.session_state.pop("mesures_snapshot", None)
                st.success(f"✅ {len(pi_vals)} tags récupérés depuis PI Server")
                st.rerun()

        if "pi_data" in st.session_state:
            v = st.session_state["pi_data"]
            st.info("🔴 Données PI Server actives")

    st.divider()
    st.header("⚙️ Paramètres simulation")
    cadence    = st.slider("Cadence SAP (%)", 0, 100, int(v["cadence"]))
    Q_HP_dispo = round((cadence / 100.0) * P["Q_HP_max"], 1)
    st.info(f"Débit HP disponible : **{Q_HP_dispo} T/h**")

    st.subheader("🟣 Clients MP (12 Bar)")
    Q_DAP_MP_client   = st.number_input("DAP — MP (T/h)",           0.0, 10.0,  v["Q_DAP_MP"],    0.1)
    Q_ejecteur_client = st.number_input("Éjecteur — MP (T/h)",      0.0, 5.0,   v["Q_ejecteur"],  0.1)
    Q_JPH_transf      = st.number_input("JPH Transfert — MP (T/h)", 0.0, 80.0,  v["Q_JPH_transf"],0.1)
    D_MP = Q_DAP_MP_client + Q_ejecteur_client + Q_JPH_transf
    st.info(f"Demande MP : **{D_MP:.2f} T/h**")

    st.subheader("🔵 Clients BP (5 Bar)")
    Q_BP_J          = st.number_input("Client J — BP (T/h)", 0.0, 40.0, v["Q_BP_J"], 1.0)
    Q_BP_K          = st.number_input("Client K — BP (T/h)", 0.0, 40.0, v["Q_BP_K"], 1.0)
    Q_BP_L          = st.number_input("Client L — BP (T/h)", 0.0, 40.0, v["Q_BP_L"], 1.0)
    Q_DAP_BP_client = st.number_input("DAP — BP (T/h)",      0.0, 8.0, v["Q_DAP_BP"], 0.1)
    D_BP = Q_BP_J + Q_BP_K + Q_BP_L + Q_DAP_BP_client
    st.info(f"Demande BP : **{D_BP:.2f} T/h**")

    st.subheader("📥 Apports externes")
    Q_JPH_recep = st.number_input("JPH Réception → MP (T/h)", 0.0, 80.0, v["Q_JPH_recep"], 0.5)
    Q_HRS       = st.number_input("Apport HRS → BP (T/h)",    0.0, 72.0, v["Q_HRS"],       0.5)

    st.divider()
    st.subheader("Variables de décision")
    x1 = st.number_input("x1 — Débit turbine (T/h)", 0.0, float(Q_HP_dispo), min(v["x1"], Q_HP_dispo), 1.0)
    x2 = st.number_input("x2 — Bypass HP→MP (T/h)",  0.0, float(Q_HP_dispo), v["x2"],  0.5)
    x3 = max(0.0, round(Q_HP_dispo - x1 - x2, 2))
    st.number_input(
        "x3 — Bypass HP→BP (calculé, T/h)",
        value=float(x3),
        disabled=True,
        format="%.2f",
        help="= Q_HP − x1 − x2  (FI-203 − FI-151 − FI-005)"
    )
    x4 = st.number_input("x4 — Soutirage MP (T/h)",  0.0, 25.0,  v["x4"],  0.5)
    x5 = st.number_input("x5 — Soutirage LP (T/h)",  0.0, 130.0, v["x5"],  0.5)

    # Mesures tags pour diagnostic automatique
    mesures_auto = {
        "x1":             v["x1"],         # clé harmonisée pour diagnostiquer()
        "x1_turbine":     v["x1"],         # alias pour compatibilité
        "P_HP_mes":       v["P_HP"],
        "T_HP_mes":       v["T_HP"],
        "Q_HP_mes":       v["cadence"] / 100.0 * P["Q_HP_max"],
        "P_elec_mes":     v["P_elec_mes"],
        "Q_HP_turb_mes":  v["x1"],         # Débit HP entrée turbine = x1 (FI-151)
        "T_HP_turb_mes":  v["T_HP"],       # T° HP entrée turbine ≈ TIC-327 (TI-154)
        "N_turb_mes":     v["N_turb"],
        "Q_sout_MP_mes":  v["x4"],
        "Q_sout_LP_mes":  v["x5"],
        "Q_det_MP_mes":   v["x2"],
        "P_cond_mes":     v["P_cond"],
        "T_cond_mes":     v["T_cond"],
        "pct_PV004_mes":  v["pct_PV004"],
        "pct_PV024_mes":  v["pct_PV024"],
        "pct_ATM1_mes":   v["pct_ATM1"],
        "pct_ATM2_mes":   v["pct_ATM2"],
    }


# ─────────────────────────────────────────────
#  CALCUL BILAN
# ─────────────────────────────────────────────
B = bilan_complet(cadence, x1, x2, x3, x4, x5, D_MP, D_BP, Q_JPH_recep, Q_HRS)

# ─────────────────────────────────────────────
#  NAVIGATION RAPIDE — Raccourci onglet
# ─────────────────────────────────────────────
TAB_NAMES = [
    "Bilan Énergétique",
    "Analyse des Pertes",
    "Diagnostic",
    "Optimisation Pareto",
    "Historique",
]

# ─────────────────────────────────────────────
#  ONGLETS
# ─────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs(TAB_NAMES)

# ══════════════════════════════════════════════
# TAB 1 — BILAN ÉNERGÉTIQUE
# ══════════════════════════════════════════════





with tab1:
    st.markdown('<p class="section-title">Bilan Énergétique</p>', unsafe_allow_html=True)

    # ── Efficacité électrique = P_elec / E_turbine_entrée (x1 seul)
    E_turb_MW = x1 * P["h_HP"] / 3600
    eta_elec  = (B['P_elec_MW'] / E_turb_MW * 100) if E_turb_MW > 0 else 0.0

    # ── Efficacité globale cogénération = (P_elec + E_sout_MP_utile + E_sout_BP_utile) / E_HP_entree
    # Énergie utile soutirage = débit × (h_vapeur - h_condensat) : seule la partie > condensat est valorisée
    E_sout_MP_utile   = x4 * (P["h_MP"] - P["h_condensat"]) / 3600
    E_sout_BP_utile   = x5 * (P["h_BP"] - P["h_condensat"]) / 3600
    E_utile_tot_MW    = B['P_elec_MW'] + E_sout_MP_utile + E_sout_BP_utile
    E_tot_MW          = B['E_entree_MW']  # source unique = vapeur HP entrante
    eta_glob_centrale = (E_utile_tot_MW / E_tot_MW * 100) if E_tot_MW > 0 else 0.0

    # ── Barres de progression pour KPIs ────────────────────────────
    P_elec_pct      = min(B['P_elec_MW'] / 63.0 * 100, 100)
    eta_elec_pct    = min(eta_elec / 40.0 * 100, 100)
    eta_glob_pct    = min(eta_glob_centrale / 40.0 * 100, 100)
    E_HP_pct        = min(B['E_entree_MW'] / 250.0 * 100, 100)

    # Couleur barre selon seuil
    # bar_color local: seuils spécifiques tab Bilan (75%/45%)
    def bar_color(pct):  # noqa: F811 — seuils différents de la version globale
        if pct >= 75: return "#1a7a1a"
        if pct >= 45: return "#f9a825"
        return "#c62828"

    st.markdown(f"""
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.2rem;">

      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(0,100,0,0.09);
                  border:1px solid #c8e6c9;border-top:3px solid {bar_color(P_elec_pct)};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;color:#145214;font-weight:700;font-style:normal;margin-bottom:0.3rem;">
          Puissance électrique</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;font-weight:900;color:#145214;line-height:1.1;">
          {B['P_elec_MW']:.3f} <span style="font-size:1.1rem;font-weight:700;">MW</span></div>
        <div style="margin-bottom:0.6rem;"></div>
        <div style="background:#e8f5e9;border-radius:6px;height:7px;">
          <div style="background:{bar_color(P_elec_pct)};width:{P_elec_pct:.1f}%;height:7px;border-radius:6px;transition:width 0.4s;"></div>
        </div>
      </div>

      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(0,100,0,0.09);
                  border:1px solid #c8e6c9;border-top:3px solid {bar_color(eta_elec_pct)};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;color:#145214;font-weight:700;font-style:normal;margin-bottom:0.3rem;">
          Efficacité électrique</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;font-weight:900;color:#145214;line-height:1.1;">
          {eta_elec:.2f} <span style="font-size:1.1rem;font-weight:700;">%</span></div>
        <div style="margin-bottom:0.6rem;"></div>
        <div style="background:#e8f5e9;border-radius:6px;height:7px;">
          <div style="background:{bar_color(eta_elec_pct)};width:{eta_elec_pct:.1f}%;height:7px;border-radius:6px;"></div>
        </div>
      </div>

      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(0,100,0,0.09);
                  border:1px solid #c8e6c9;border-top:3px solid {bar_color(eta_glob_pct)};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;color:#145214;font-weight:700;font-style:normal;margin-bottom:0.3rem;">
          Efficacité globale η</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;font-weight:900;color:#145214;line-height:1.1;">
          {eta_glob_centrale:.2f} <span style="font-size:1.1rem;font-weight:700;">%</span></div>
        <div style="margin-bottom:0.6rem;"></div>
        <div style="background:#e8f5e9;border-radius:6px;height:7px;">
          <div style="background:{bar_color(eta_glob_pct)};width:{eta_glob_pct:.1f}%;height:7px;border-radius:6px;"></div>
        </div>
      </div>

      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(0,100,0,0.09);
                  border:1px solid #c8e6c9;border-top:3px solid {bar_color(E_HP_pct)};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;color:#145214;font-weight:700;font-style:normal;margin-bottom:0.3rem;">
          Énergie entrante HP</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;font-weight:900;color:#145214;line-height:1.1;">
          {B['E_entree_MW']:.1f} <span style="font-size:1.1rem;font-weight:700;">MW</span></div>
        <div style="margin-bottom:0.6rem;"></div>
        <div style="background:#e8f5e9;border-radius:6px;height:7px;">
          <div style="background:{bar_color(E_HP_pct)};width:{E_HP_pct:.1f}%;height:7px;border-radius:6px;"></div>
        </div>
      </div>

    </div>
    """, unsafe_allow_html=True)

    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        st.markdown('<p class="section-title">Bilans massiques — 4 nœuds</p>', unsafe_allow_html=True)

        def color_ecart(val):
            if not isinstance(val, float): return ''
            if abs(val) < 0.1:  return 'background-color:#d4edda'
            if abs(val) < 2.0:  return 'background-color:#fff3cd'
            return 'background-color:#f8d7da'

        df_bil = pd.DataFrame([
            {"Nœud": "HP",          "Entrée (T/h)": B["Q_HP_dispo"],      "Sortie (T/h)": round(x1+x2+x3, 2), "Écart (T/h)": round(B["bilan_HP"], 3)},
            {"Nœud": "Turbine",     "Entrée (T/h)": round(x1, 2),         "Sortie (T/h)": round(x4+x5+B["x6"], 2), "Écart (T/h)": round(B["bilan_turb"], 3)},
            {"Nœud": "MP (12 Bar)", "Entrée (T/h)": round(B["alim_MP"], 2),"Sortie (T/h)": round(D_MP, 2),     "Écart (T/h)": round(B["ecart_MP"], 2)},
            {"Nœud": "BP (5 Bar)",  "Entrée (T/h)": round(B["alim_BP"], 2),"Sortie (T/h)": round(D_BP, 2),     "Écart (T/h)": round(B["ecart_BP"], 2)},
        ])
        # Tableau bilan massique HTML stylé
        def color_ecart_bg(val):
            try:
                v = float(val)
                if abs(v) < 0.1:  return "#d5f5e3"
                if abs(v) < 2.0:  return "#fdebd0"
                return "#fadbd8"
            except: return "white"

        bil_html = ""
        for _, r in df_bil.iterrows():
            ec_bg = color_ecart_bg(r["Écart (T/h)"])
            bil_html += (
                f'<tr style="border-bottom:1px solid #e8f5e9;">' +
                f'<td style="padding:0.3rem 0.7rem;font-family:Times New Roman,Times,serif;font-size:0.9rem;">{r["Nœud"]}</td>' +
                f'<td style="padding:0.3rem 0.7rem;text-align:right;font-family:Times New Roman,Times,serif;font-size:0.9rem;">{r["Entrée (T/h)"]}</td>' +
                f'<td style="padding:0.3rem 0.7rem;text-align:right;font-family:Times New Roman,Times,serif;font-size:0.9rem;">{r["Sortie (T/h)"]}</td>' +
                f'<td style="padding:0.3rem 0.7rem;text-align:right;font-family:Times New Roman,Times,serif;font-size:0.9rem;background:{ec_bg};font-weight:700;">{r["Écart (T/h)"]}</td>' +
                f'</tr>'
            )
        bil_table = (
            '<table style="width:100%;border-collapse:collapse;font-family:Times New Roman,Times,serif;' +
            'font-size:0.92rem;border:1px solid #c8e6c9;border-radius:8px;overflow:hidden;margin-top:0.4rem;">' +
            '<thead><tr style="background:#2e6da4;color:white;">' +
            '<th style="padding:0.5rem 0.8rem;text-align:left;font-weight:700;">Nœud</th>' +
            '<th style="padding:0.5rem 0.8rem;text-align:right;font-weight:700;">Entrée (T/h)</th>' +
            '<th style="padding:0.5rem 0.8rem;text-align:right;font-weight:700;">Sortie (T/h)</th>' +
            '<th style="padding:0.5rem 0.8rem;text-align:right;font-weight:700;">Écart (T/h)</th>' +
            f'</tr></thead><tbody>{bil_html}</tbody></table>'
        )
        st.markdown(bil_table, unsafe_allow_html=True)

        st.markdown('<p class="section-title">Détail clients MP</p>', unsafe_allow_html=True)
        df_mp = pd.DataFrame([
            {"Client MP": "DAP",               "T/h": Q_DAP_MP_client},
            {"Client MP": "Éjecteur",           "T/h": Q_ejecteur_client},
            {"Client MP": "JPH Transfert",      "T/h": Q_JPH_transf},
            {"Client MP": "JPH Réception (↑apport)", "T/h": f"+{Q_JPH_recep:.2f}"},
            {"Client MP": "TOTAL NET",          "T/h": round(D_MP, 2)},
        ])
        def render_table(rows_data, col1_name, col2_name):
            rows_html = ""
            for r in rows_data:
                is_total = str(r[0]) == "TOTAL NET"
                bg  = "#c8e6c9" if is_total else "white"
                fw  = "900"     if is_total else "400"
                clr = "#145214" if is_total else "#222"
                rows_html += f'<tr style="background:{bg};">'\
                    f'<td style="padding:0.42rem 0.8rem;font-weight:{fw};color:{clr};border-bottom:1px solid #e8f5e9;">{r[0]}</td>'\
                    f'<td style="padding:0.42rem 0.8rem;font-weight:{fw};color:{clr};text-align:right;border-bottom:1px solid #e8f5e9;">{r[1]}</td></tr>'
            return (f'<table style="width:100%;border-collapse:collapse;font-family:Times New Roman,Times,serif;'
                    f'font-size:0.92rem;border:1px solid #c8e6c9;border-radius:8px;overflow:hidden;margin-top:0.4rem;">'
                    f'<thead><tr style="background:#2e6da4;color:white;">'
                    f'<th style="padding:0.5rem 0.8rem;text-align:left;font-weight:700;letter-spacing:0.03em;">{col1_name}</th>'
                    f'<th style="padding:0.5rem 0.8rem;text-align:right;font-weight:700;letter-spacing:0.03em;">{col2_name}</th>'
                    f'</tr></thead><tbody>{rows_html}</tbody></table>')
        mp_rows = [(r["Client MP"], r["T/h"]) for _, r in df_mp.iterrows()]
        st.markdown(render_table(mp_rows, "Client MP", "T/h"), unsafe_allow_html=True)

        st.markdown('<p class="section-title">Détail clients BP</p>', unsafe_allow_html=True)
        df_bp = pd.DataFrame([
            {"Client BP": "Client J",           "T/h": Q_BP_J},
            {"Client BP": "Client K",           "T/h": Q_BP_K},
            {"Client BP": "Client L",           "T/h": Q_BP_L},
            {"Client BP": "DAP",                "T/h": Q_DAP_BP_client},
            {"Client BP": "HRS (↑apport)",      "T/h": f"+{Q_HRS:.2f}"},
            {"Client BP": "TOTAL NET",          "T/h": round(D_BP, 2)},
        ])
        bp_rows = [(r["Client BP"], r["T/h"]) for _, r in df_bp.iterrows()]
        st.markdown(render_table(bp_rows, "Client BP", "T/h"), unsafe_allow_html=True)

    with col2:
        st.markdown('<p class="section-title">Répartition débit vapeur HP</p>', unsafe_allow_html=True)
        pie_labels = ["x1 — Turbine", "x2 — Bypass MP", "x3 — Bypass BP"]
        pie_values = [x1, x2, x3]
        pie_colors = ["#2471a3", "#e67e22", "#c0392b"]
        if x1 + x2 + x3 == 0:
            pie_values = [1, 0, 0]
        fig_pie = go.Figure(go.Pie(
            labels=pie_labels, values=pie_values, hole=0.45,
            marker=dict(colors=pie_colors),
            textinfo="label+percent+value",
            textposition="outside",
            hovertemplate="%{label}<br>%{value:.1f} T/h<br>%{percent}<extra></extra>"
        ))
        fig_pie.update_layout(height=300, margin=dict(l=0, r=0, t=20, b=0),
                              showlegend=True, legend=dict(orientation="h", y=-0.1),
                              paper_bgcolor="rgba(0,0,0,0)",
                              font=dict(family="Times New Roman, Times, serif"))
        st.plotly_chart(fig_pie, use_container_width=True)

        # Gauge en face du tableau BP
        st.markdown('<p class="section-title" style="margin-top:4.5rem;">Puissance électrique produite</p>', unsafe_allow_html=True)
        fig_g = go.Figure(go.Indicator(
            mode="gauge+number+delta",
            value=B["P_elec_MW"],
            delta={"reference": 52.502, "suffix": " MW"},
            number={"suffix": " MW", "font": {"size": 32, "family": "Times New Roman, Times, serif", "color": "#1a3a6e"}},
            gauge={
                "axis": {"range": [0, 63], "ticksuffix": ""},
                "bar":  {"color": "#1a3a6e"},
                "steps": [
                    {"range": [0, 30],  "color": "#fadbd8"},
                    {"range": [30, 45], "color": "#fdebd0"},
                    {"range": [45, 63], "color": "#d6eaf8"},
                ],
                "threshold": {"line": {"color": "red", "width": 3}, "value": 52.502},
            },
        ))
        fig_g.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=20, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Times New Roman, Times, serif", color="#1a3a6e")
        )
        st.plotly_chart(fig_g, use_container_width=True)

# ══════════════════════════════════════════════
# TAB 2 — ANALYSE DES PERTES
# ══════════════════════════════════════════════
with tab2:
    st.markdown('<p class="section-title">Analyse des Pertes Énergétiques</p>', unsafe_allow_html=True)

    # ── Calculs corrigés ────────────────────────────────────────────
    E_entree_MW  = B["E_entree_MW"]                                    # Énergie HP entrante
    P_elec_MW    = B["P_elec_MW"]                                      # Puissance électrique
    P_cond_MW    = max(0, B["x6"] * (P["h_HP"] - P["h_exhaust"]) / 3600)  # Pertes condenseur
    P_by_MP_MW   = x2 * (P["h_HP"] - P["h_MP"]) / 3600               # Pertes bypass HP→MP
    P_by_BP_MW   = x3 * (P["h_HP"] - P["h_BP"]) / 3600               # Pertes bypass HP→BP
    E_sout_MP_MW = x4 * P["h_MP"] / 3600                              # Énergie valorisée soutirage MP
    E_sout_BP_MW = x5 * P["h_BP"] / 3600                              # Énergie valorisée soutirage BP
    # Pertes réelles = ce qui reste après valorisation
    P_pertes_reelles_MW = max(0, E_entree_MW - P_elec_MW - P_cond_MW
                               - P_by_MP_MW - P_by_BP_MW
                               - E_sout_MP_MW - E_sout_BP_MW)
    E_valorisee_MW  = P_elec_MW + E_sout_MP_MW + E_sout_BP_MW         # Total valorisé
    P_pertes_tot_MW = E_entree_MW - E_valorisee_MW                    # Pertes totales
    taux_pertes_pct = (P_pertes_tot_MW / E_entree_MW * 100) if E_entree_MW > 0 else 0

    # ── KPI cards ───────────────────────────────────────────────────
    # bar_color2: utilise les helpers globaux bar_color / bar_color_inv
    def bar_color2(pct, inverse=False):
        return bar_color_inv(pct) if inverse else bar_color(pct)

    E_val_pct   = min(E_valorisee_MW  / E_entree_MW * 100, 100) if E_entree_MW > 0 else 0
    P_pct       = min(P_pertes_tot_MW / E_entree_MW * 100, 100) if E_entree_MW > 0 else 0

    # ── Couleurs KPI ────────────────────────────────────────────────
    c_orange = "#e07b1a"   # orange — énergie entrante
    c_val    = "#1a7a1a"   # vert   — valorisée
    c_pert   = "#e53935"   # rouge doux — pertes totales
    c_warn   = "#e53935"   # rouge doux — taux pertes

    st.markdown(f"""
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1.5rem;">

      <!-- KPI 1: Énergie entrante -->
      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(200,100,0,0.10);
                  border:1px solid #fde8c8;border-top:3px solid {c_orange};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                    font-weight:700;color:#222;margin-bottom:0.35rem;">
          Énergie entrante HP</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;
                    font-weight:900;color:#111;line-height:1.1;">
          {E_entree_MW:.1f}<span style="font-size:1.05rem;font-weight:700;margin-left:5px;">MW</span></div>
        <div style="background:#fef3e2;border-radius:6px;height:5px;margin-top:0.7rem;">
          <div style="background:{c_orange};width:100%;height:5px;border-radius:6px;"></div>
        </div>
      </div>

      <!-- KPI 2: Énergie valorisée -->
      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(0,100,0,0.09);
                  border:1px solid #c8e6c9;border-top:3px solid {c_val};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                    font-weight:700;color:#222;margin-bottom:0.35rem;">
          Énergie valorisée</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;
                    font-weight:900;color:#111;line-height:1.1;">
          {E_valorisee_MW:.1f}<span style="font-size:1.05rem;font-weight:700;margin-left:5px;">MW</span></div>
        <div style="background:#e8f5e9;border-radius:6px;height:5px;margin-top:0.7rem;">
          <div style="background:{c_val};width:{E_val_pct:.1f}%;height:5px;border-radius:6px;"></div>
        </div>
      </div>

      <!-- KPI 3: Pertes totales -->
      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(220,50,50,0.09);
                  border:1px solid #fccfcf;border-top:3px solid {c_pert};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                    font-weight:700;color:#222;margin-bottom:0.35rem;">
          Pertes totales</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;
                    font-weight:900;color:#111;line-height:1.1;">
          {P_pertes_tot_MW:.1f}<span style="font-size:1.05rem;font-weight:700;margin-left:5px;">MW</span></div>
        <div style="background:#fdecea;border-radius:6px;height:5px;margin-top:0.7rem;">
          <div style="background:{c_pert};width:{P_pct:.1f}%;height:5px;border-radius:6px;"></div>
        </div>
      </div>

      <!-- KPI 4: Taux de pertes -->
      <div style="background:white;border-radius:12px;padding:1.1rem 1.2rem;
                  box-shadow:0 2px 10px rgba(220,50,50,0.09);
                  border:1px solid #fccfcf;border-top:3px solid {c_warn};">
        <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                    font-weight:700;color:#222;margin-bottom:0.35rem;">
          Taux de pertes</div>
        <div style="font-family:'Times New Roman',Times,serif;font-size:2rem;
                    font-weight:900;color:#111;line-height:1.1;">
          {taux_pertes_pct:.1f}<span style="font-size:1.05rem;font-weight:700;margin-left:5px;">%</span></div>
        <div style="background:#fdecea;border-radius:6px;height:5px;margin-top:0.7rem;">
          <div style="background:{c_warn};width:{min(taux_pertes_pct,100):.1f}%;height:5px;border-radius:6px;"></div>
        </div>
      </div>

    </div>
    """, unsafe_allow_html=True)

    # ── Ligne 2: Tableau détail + Donut ─────────────────────────────
    col1, col2 = st.columns([1.1, 1])

    with col1:
        st.markdown('<p class="section-title">Bilan énergétique détaillé</p>', unsafe_allow_html=True)
        postes = [
            ("Puissance électrique",       P_elec_MW,          "#1a7a1a", "Valorisée"),
            ("Soutirage MP (valorisé)",     E_sout_MP_MW,       "#2e6da4", "Valorisée"),
            ("Soutirage BP (valorisé)",     E_sout_BP_MW,       "#5ba3e0", "Valorisée"),
            ("Pertes condenseur",           P_cond_MW,          "#e74c3c", "Perte"),
            ("Pertes bypass HP→MP",         P_by_MP_MW,         "#e67e22", "Perte"),
            ("Pertes bypass HP→BP",         P_by_BP_MW,         "#c0392b", "Perte"),
            ("Pertes mécaniques/autres",    P_pertes_reelles_MW,"#888888", "Perte"),
        ]
        # Construire tableau HTML
        rows_html = ""
        for nom, val, clr, typ in postes:
            pct = val / E_entree_MW * 100 if E_entree_MW > 0 else 0
            typ_bg  = "#e8f5e9" if typ == "Valorisée" else "#fdecea"
            typ_clr = "#145214" if typ == "Valorisée" else "#c62828"
            rows_html += (
                f'<tr style="border-bottom:1px solid #f0f0f0;">' +
                f'<td style="padding:0.35rem 0.7rem;font-family:Times New Roman,serif;font-size:0.88rem;">' +
                f'<span style="display:inline-block;width:10px;height:10px;background:{clr};border-radius:50%;margin-right:6px;"></span>{nom}</td>' +
                f'<td style="padding:0.35rem 0.7rem;text-align:right;font-family:Times New Roman,serif;font-size:0.88rem;font-weight:700;">{val:.2f}</td>' +
                f'<td style="padding:0.35rem 0.7rem;text-align:right;font-family:Times New Roman,serif;font-size:0.88rem;">{pct:.1f}%</td>' +
                f'<td style="padding:0.35rem 0.7rem;text-align:center;">' +
                f'<span style="background:{typ_bg};color:{typ_clr};font-size:0.75rem;font-weight:700;' +
                f'padding:2px 8px;border-radius:10px;font-family:Times New Roman,serif;">{typ}</span></td>' +
                f'</tr>'
            )
        # Ligne total
        rows_html += (
            '<tr style="background:#f0f7f0;font-weight:900;border-top:2px solid #2e6da4;">' +
            '<td style="padding:0.4rem 0.7rem;font-family:Times New Roman,serif;font-size:0.88rem;font-weight:900;">TOTAL</td>' +
            f'<td style="padding:0.4rem 0.7rem;text-align:right;font-family:Times New Roman,serif;font-size:0.88rem;font-weight:900;">{E_entree_MW:.2f}</td>' +
            '<td style="padding:0.4rem 0.7rem;text-align:right;font-family:Times New Roman,serif;font-size:0.88rem;font-weight:900;">100%</td>' +
            '<td></td></tr>'
        )
        table_html = (
            '<table style="width:100%;border-collapse:collapse;font-family:Times New Roman,serif;' +
            'border:1px solid #c8e6c9;border-radius:8px;overflow:hidden;">' +
            '<thead><tr style="background:#2e6da4;color:white;">' +
            '<th style="padding:0.45rem 0.7rem;text-align:left;font-size:0.85rem;">Poste</th>' +
            '<th style="padding:0.45rem 0.7rem;text-align:right;font-size:0.85rem;">MW</th>' +
            '<th style="padding:0.45rem 0.7rem;text-align:right;font-size:0.85rem;">% HP</th>' +
            '<th style="padding:0.45rem 0.7rem;text-align:center;font-size:0.85rem;">Type</th>' +
            f'</tr></thead><tbody>{rows_html}</tbody></table>'
        )
        st.markdown(table_html, unsafe_allow_html=True)

    with col2:
        st.markdown('<p class="section-title">Répartition énergétique</p>', unsafe_allow_html=True)
        donut_labels = [p[0] for p in postes if p[1] > 0]
        donut_values = [p[1] for p in postes if p[1] > 0]
        donut_colors = [p[2] for p in postes if p[1] > 0]
        fig_donut = go.Figure(go.Pie(
            labels=donut_labels,
            values=donut_values,
            hole=0.42,
            marker=dict(colors=donut_colors, line=dict(color="white", width=1.5)),
            textinfo="percent",
            textposition="inside",
            hovertemplate="<b>%{label}</b><br>%{value:.2f} MW<br>%{percent}<extra></extra>",
        ))
        fig_donut.update_layout(
            height=360,
            margin=dict(l=0, r=0, t=10, b=10),
            showlegend=True,
            legend=dict(orientation="v", x=1.01, y=0.5,
                        font=dict(family="Times New Roman, serif", size=11)),
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Times New Roman, serif")
        )
        st.plotly_chart(fig_donut, use_container_width=True)

    # ── Sankey diagram ───────────────────────────────────────────────
    st.markdown('<p class="section-title">Flux énergétique — Diagramme de Sankey</p>', unsafe_allow_html=True)

    # Noeuds: 0=HP_entrée, 1=Turbine, 2=Élec, 3=Condenseur,
    #         4=Bypass_MP, 5=Bypass_BP, 6=Sout_MP, 7=Sout_BP, 8=Pertes_méca
    sk_nodes = ["Énergie HP", "Turbine", "Électricité",
                "Condenseur", "Bypass HP→MP", "Bypass HP→BP",
                "Soutirage MP", "Soutirage BP", "Pertes mécaniques"]
    sk_colors_node = ["#1a3a6e","#2e6da4","#1a7a1a",
                      "#c0392b","#e67e22","#922b21",
                      "#2980b9","#5dade2","#7f8c8d"]

    # Links: source → target, value
    sk_links = []
    # HP → Turbine (x1)
    if x1 > 0:
        sk_links.append((0, 1, round(x1 * P["h_HP"] / 3600, 2)))
    # HP → Bypass_MP (x2)
    if P_by_MP_MW > 0.01:
        sk_links.append((0, 4, round(P_by_MP_MW, 2)))
    # HP → Bypass_BP (x3)
    if P_by_BP_MW > 0.01:
        sk_links.append((0, 5, round(P_by_BP_MW, 2)))
    # HP → Soutirage MP (x4)
    if E_sout_MP_MW > 0.01:
        sk_links.append((0, 6, round(E_sout_MP_MW, 2)))
    # HP → Soutirage BP (x5)
    if E_sout_BP_MW > 0.01:
        sk_links.append((0, 7, round(E_sout_BP_MW, 2)))
    # Turbine → Électricité
    if P_elec_MW > 0:
        sk_links.append((1, 2, round(P_elec_MW, 2)))
    # Turbine → Condenseur
    if P_cond_MW > 0.01:
        sk_links.append((1, 3, round(P_cond_MW, 2)))
    # Turbine → Pertes mécaniques
    if P_pertes_reelles_MW > 0.01:
        sk_links.append((1, 8, round(P_pertes_reelles_MW, 2)))

    link_colors = ["rgba(46,109,164,0.35)","rgba(231,76,60,0.35)","rgba(192,57,43,0.35)",
                   "rgba(46,109,164,0.35)","rgba(91,163,224,0.35)",
                   "rgba(26,122,26,0.4)","rgba(231,76,60,0.4)","rgba(136,136,136,0.35)"]

    fig_sk = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=22, thickness=24,
            line=dict(color="white", width=0.8),
            label=sk_nodes,
            color=sk_colors_node,
            hovertemplate="<b>%{label}</b><br>%{value:.2f} MW<extra></extra>"
        ),
        link=dict(
            source=[l[0] for l in sk_links],
            target=[l[1] for l in sk_links],
            value =[l[2] for l in sk_links],
            color =link_colors[:len(sk_links)],
            hovertemplate="%{source.label} → %{target.label}<br>%{value:.2f} MW<extra></extra>"
        )
    ))
    fig_sk.update_layout(
        height=400,
        margin=dict(l=20, r=20, t=15, b=15),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Times New Roman, Times, serif", size=13, color="#111111")
    )
    st.plotly_chart(fig_sk, use_container_width=True)

# ══════════════════════════════════════════════
# TAB 3 — DIAGNOSTIC TAGS PI
# ══════════════════════════════════════════════
with tab3:
    st.markdown('<p class="section-title">Diagnostic — Centrale Thermique 515A</p>', unsafe_allow_html=True)

    # ── Mode source ──────────────────────────────────────────────────
    has_auto = "excel_data" in st.session_state or "pi_data" in st.session_state
    mesures_affich = mesures_auto if has_auto else {
        code: float(info["nominal"]) for code, info in TAGS.items()
        if info["nominal"] is not None
    }
    # Snapshot figé au dernier diagnostic (stable entre reruns)
    mesures_diag = st.session_state.get("mesures_snapshot", mesures_affich)

    # ── Bandeau source ───────────────────────────────────────────────
    if has_auto:
        st.markdown("""<div style="background:#e8f5e9;border-left:4px solid #1a7a1a;border-radius:6px;
        padding:0.6rem 1rem;font-family:'Times New Roman',Times,serif;font-size:0.9rem;color:#145214;
        margin-bottom:1rem;">Valeurs chargées automatiquement depuis la source de données connectée.</div>""",
        unsafe_allow_html=True)
    else:
        st.markdown("""<div style="background:#fff8e1;border-left:4px solid #f9a825;border-radius:6px;
        padding:0.6rem 1rem;font-family:'Times New Roman',Times,serif;font-size:0.9rem;color:#7d5a00;
        margin-bottom:1rem;">Aucune source connectée — valeurs nominales affichées à titre indicatif.
        Chargez un fichier Excel ou connectez le PI Server pour un diagnostic réel.</div>""",
        unsafe_allow_html=True)

    # ── Définition catégories ────────────────────────────────────────
    CATS = {
        "HP":     ("Vapeur Haute Pression",  "#e53935"),
        "TRB":    ("Turbine",               "#e67e22"),
        "MP":     ("Soutirage MP — 12 Bar", "#8e44ad"),
        "BP":     ("Soutirage BP — 5 Bar",  "#2980b9"),
        "BYPASS": ("Bypasses & Détentes",   "#f9a825"),
        "COND":   ("Condenseur",            "#795548"),
        "VANNE":  ("Vannes de Régulation",  "#546e7a"),
        "ARROS":  ("Arrosage",              "#00897b"),
        "CONSO":  ("Consommateurs",         "#1a7a1a"),
        "ATM":    ("Sécurité ATM",          "#b71c1c"),
    }

    # ── Affichage tags par catégorie ─────────────────────────────────
    for cat_key, (cat_label, cat_color) in CATS.items():
        tags_cat_all = {k: v for k, v in TAGS.items() if v["cat"] == cat_key}
        if not tags_cat_all:
            continue

        # Compter alertes dans cette catégorie
        n_crit = 0; n_warn = 0
        for code, info in tags_cat_all.items():
            val = mesures_affich.get(code)
            nom = info["nominal"]
            if val is None or nom is None:
                continue
            # ATM: nominal=0 mais khas ihsab
            if nom == 0:
                if code in ("pct_ATM1_mes", "pct_ATM2_mes"):
                    if val > 20:   n_crit += 1
                    elif val > 0:  n_warn += 1
                continue
            ec = abs((val - nom) / nom * 100)
            if ec >= 10: n_crit += 1
            elif ec >= 5: n_warn += 1

        badge = ""
        if n_crit > 0:
            badge = f' &nbsp;<span style="background:#e53935;color:white;font-size:0.7rem;padding:1px 7px;border-radius:10px;font-weight:700;">{n_crit} CRITIQUE</span>'
        elif n_warn > 0:
            badge = f' &nbsp;<span style="background:#f9a825;color:white;font-size:0.7rem;padding:1px 7px;border-radius:10px;font-weight:700;">{n_warn} WARNING</span>'

        header_html = (
            f'<span style="font-family:Times New Roman,Times,serif;font-weight:700;font-size:0.95rem;">' +
            f'<span style="display:inline-block;width:10px;height:10px;background:{cat_color};' +
            f'border-radius:50%;margin-right:8px;vertical-align:middle;"></span>{cat_label}</span>{badge}'
        )


        with st.expander(cat_label, expanded=(n_crit > 0)):
            st.markdown(
                f'<div style="font-family:Times New Roman,Times,serif;font-weight:700;font-size:0.95rem;' +
                f'color:{cat_color};margin-bottom:0.8rem;border-bottom:1px solid #eee;padding-bottom:0.4rem;">' +
                f'<span style="display:inline-block;width:10px;height:10px;background:{cat_color};' +
                f'border-radius:50%;margin-right:8px;vertical-align:middle;"></span>{cat_label}</div>',
                unsafe_allow_html=True
            )
            cols = st.columns(3)
            for i, (code, info) in enumerate(tags_cat_all.items()):
                with cols[i % 3]:
                    tag_short    = info["tag"].split(".")[-1]
                    nominal      = info["nominal"]
                    val_actuelle = mesures_affich.get(code)
                    if val_actuelle is None:
                        val_actuelle = nominal

                    # Couleur fond selon écart
                    if nominal is not None and nominal != 0 and val_actuelle is not None:
                        ec = abs((val_actuelle - nominal) / nominal * 100)
                        if ec >= 10:   bg, brd, lbl = "#fdecea", "#e53935", "CRITIQUE"
                        elif ec >= 5:  bg, brd, lbl = "#fff8e1", "#f9a825", "WARNING"
                        else:          bg, brd, lbl = "#f1f8f1", "#81c784", "OK"
                    else:
                        bg, brd, lbl = "#f5f5f5", "#bdbdbd", "—"

                    val_str = f"{val_actuelle:.2f} {info['unite']}" if val_actuelle is not None else "N/A"

                    st.markdown(f"""
                    <div style="background:white;border:1px solid #e8edf2;border-radius:10px;
                                border-left:4px solid {brd};
                                padding:0.6rem 0.8rem;margin-bottom:0.5rem;
                                box-shadow:0 1px 4px rgba(0,0,0,0.06);">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.25rem;">
                            <span style="font-family:'Times New Roman',Times,serif;font-size:0.78rem;
                                         color:#444;line-height:1.2;">{info["nom"]}</span>
                            <span style="font-family:monospace;font-size:0.7rem;color:#1a3a6e;
                                         background:#eef2f7;border-radius:3px;padding:1px 5px;
                                         white-space:nowrap;margin-left:6px;">{tag_short}</span>
                        </div>
                        <div style="font-family:'Times New Roman',Times,serif;font-size:1.2rem;
                                    font-weight:900;color:#111;">{val_str}</div>
                    </div>
                    """, unsafe_allow_html=True)

    # ── Bouton lancer ────────────────────────────────────────────────
    st.markdown("<div style='margin-top:1rem;'>", unsafe_allow_html=True)
    # Init historique depuis fichier JSON (persistant)
    if "hist_diag" not in st.session_state:
        st.session_state["hist_diag"] = _load_hist()

    if st.button("Lancer le diagnostic", type="primary", use_container_width=True):
        import datetime as _dt
        _res_diag = diagnostiquer(mesures_affich)
        st.session_state["alertes_diag"]    = _res_diag
        st.session_state["mesures_snapshot"] = dict(mesures_affich)
        _save_last_diag(_res_diag, dict(mesures_affich))
        _nc = sum(1 for a in _res_diag if "CRITIQUE" in a["statut"])
        _nw = sum(1 for a in _res_diag if "WARNING"  in a["statut"])
        _src = "PI Server" if source == "🔴 PI Server" else "Excel"
        _dedup_insert(st.session_state["hist_diag"], {
            "date":      _dt.datetime.now().strftime("%d/%m/%Y"),
            "heure":     _dt.datetime.now().strftime("%H:%M:%S"),
            "cadence":   f"{cadence:.0f}%",
            "x1":        f"{x1:.1f}",
            "source":    _src,
            "critiques": _nc,
            "warnings":  _nw,
            "statut":    "🔴 Critique" if _nc>0 else ("🟠 Warning" if _nw>0 else "🟢 OK"),
            "mesures":   dict(mesures_affich),
            "alertes":   _res_diag,
        })
        st.session_state["hist_diag"] = st.session_state["hist_diag"][:100]
        _save_hist(st.session_state["hist_diag"])
    st.markdown("</div>", unsafe_allow_html=True)

    # Restaurer depuis SQLite si session_state vide (navigation entre tabs)
    if "alertes_diag" not in st.session_state:
        _al, _mes = _load_last_diag()
        if _al is not None:
            st.session_state["alertes_diag"]    = _al
            st.session_state["mesures_snapshot"] = _mes

    alertes = st.session_state.get("alertes_diag", None)
    # Mettre à jour mesures_diag avec snapshot persisté
    if "mesures_snapshot" in st.session_state:
        mesures_diag = st.session_state["mesures_snapshot"]

    # ── Résultats diagnostic ─────────────────────────────────────────
    if alertes is not None:
        nb_crit = sum(1 for a in alertes if "CRITIQUE" in a["statut"])
        nb_warn = sum(1 for a in alertes if "WARNING"  in a["statut"])
        nb_ok   = sum(1 for a in alertes if "OK"       in a["statut"])
        nb_tot  = nb_crit + nb_warn + nb_ok

        st.divider()
        st.markdown('<p class="section-title">Résultats du diagnostic</p>', unsafe_allow_html=True)

        # KPI cards résumé
        st.markdown(f"""
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.5rem;">
          <div style="background:white;border-radius:12px;padding:1rem 1.2rem;
                      border:1px solid #fccfcf;border-top:3px solid #e53935;
                      box-shadow:0 2px 8px rgba(220,50,50,0.08);">
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;
                        font-weight:700;color:#e53935;margin-bottom:0.3rem;">Anomalies critiques</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:2.2rem;
                        font-weight:900;color:#111;">{nb_crit}</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.72rem;
                        color:#888;font-style:italic;">Écart ≥ 10% vs nominal</div>
          </div>
          <div style="background:white;border-radius:12px;padding:1rem 1.2rem;
                      border:1px solid #fff0c0;border-top:3px solid #f9a825;
                      box-shadow:0 2px 8px rgba(250,168,37,0.08);">
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;
                        font-weight:700;color:#e07b1a;margin-bottom:0.3rem;">Avertissements</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:2.2rem;
                        font-weight:900;color:#111;">{nb_warn}</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.72rem;
                        color:#888;font-style:italic;">Écart entre 5% et 10%</div>
          </div>
          <div style="background:white;border-radius:12px;padding:1rem 1.2rem;
                      border:1px solid #c8e6c9;border-top:3px solid #1a7a1a;
                      box-shadow:0 2px 8px rgba(26,122,26,0.08);">
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.88rem;
                        font-weight:700;color:#1a7a1a;margin-bottom:0.3rem;">Paramètres conformes</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:2.2rem;
                        font-weight:900;color:#111;">{nb_ok}</div>
            <div style="font-family:'Times New Roman',Times,serif;font-size:0.72rem;
                        color:#888;font-style:italic;">Écart &lt; 5% vs nominal</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        if not alertes:
            st.markdown("""<div style="background:#e8f5e9;border-left:4px solid #1a7a1a;border-radius:6px;
            padding:0.8rem 1.2rem;font-family:'Times New Roman',Times,serif;font-size:0.95rem;color:#145214;">
            Tous les paramètres sont dans les limites nominales — Centrale en fonctionnement normal.</div>""",
            unsafe_allow_html=True)
        else:
            st.markdown('<p class="section-title" style="margin-top:0.5rem;">Détail des anomalies — par ordre de gravité</p>', unsafe_allow_html=True)
            for a in alertes:
                is_crit = "CRITIQUE" in a["statut"]
                is_warn = "WARNING"  in a["statut"]
                tag_short = a["tag"].split(".")[-1]

                if is_crit:
                    card_brd, card_clr = "#e53935", "#c62828"
                    card_bg  = "#fdecea"
                    statut_lbl = "🔴 ANOMALIE CRITIQUE"
                elif is_warn:
                    card_brd, card_clr = "#f9a825", "#e07b1a"
                    card_bg  = "#fff8e1"
                    statut_lbl = "🟠 AVERTISSEMENT"
                else:
                    card_brd, card_clr = "#1a7a1a", "#2e7d32"
                    card_bg  = "#f1f8f1"
                    statut_lbl = "🟢 CONFORME"

                # Titre simple mais statut coloré via markdown avant expander
                exp_title = f"{statut_lbl} — {a['nom']}  |  Écart : {a['ecart']:+.1f}%"
                st.markdown(
                    f'<p style="font-family:Times New Roman,Times,serif;font-size:0.01rem;' +
                    f'color:transparent;margin:0;padding:0;line-height:0;"></p>',
                    unsafe_allow_html=True
                )
                with st.expander(exp_title, expanded=is_crit):
                    st.markdown(f"""
                    <div style="font-family:'Times New Roman',Times,serif;">
                      <!-- 3 cards valeurs -->
                      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.7rem;margin-bottom:0.8rem;">
                        <div style="background:#f4f8fc;border-radius:8px;padding:0.55rem 0.85rem;
                                    border-left:3px solid #2e6da4;">
                          <div style="font-size:0.72rem;color:#555;font-weight:700;margin-bottom:0.15rem;">Valeur nominale</div>
                          <div style="font-size:1.2rem;font-weight:900;color:#1a3a6e;">{a["nominal"]} <span style="font-size:0.8rem;">{a["unite"]}</span></div>
                        </div>
                        <div style="background:{card_bg};border-radius:8px;padding:0.55rem 0.85rem;
                                    border-left:3px solid {card_brd};">
                          <div style="font-size:0.72rem;color:#555;font-weight:700;margin-bottom:0.15rem;">Valeur mesurée</div>
                          <div style="font-size:1.2rem;font-weight:900;color:{card_clr};">{a["mesure"]} <span style="font-size:0.8rem;">{a["unite"]}</span></div>
                        </div>
                        <div style="background:{card_bg};border-radius:8px;padding:0.55rem 0.85rem;
                                    border-left:3px solid {card_brd};">
                          <div style="font-size:0.72rem;color:#555;font-weight:700;margin-bottom:0.15rem;">Écart relatif</div>
                          <div style="font-size:1.2rem;font-weight:900;color:{card_clr};">{a["ecart"]:+.2f} <span style="font-size:0.8rem;">%</span></div>
                        </div>
                      </div>
                    """, unsafe_allow_html=True)

                    if is_crit or is_warn:
                        st.markdown(f"""
                      <!-- Cause + Solution -->
                      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.7rem;">
                        <div style="background:#fff8e1;border-radius:8px;padding:0.6rem 0.85rem;
                                    border-left:3px solid #f9a825;">
                          <div style="font-size:0.72rem;font-weight:700;color:#7d5a00;
                                      text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.35rem;">
                            Cause probable</div>
                          <div style="font-size:0.85rem;color:#333;line-height:1.55;">{a["cause"]}</div>
                        </div>
                        <div style="background:#e8f5e9;border-radius:8px;padding:0.6rem 0.85rem;
                                    border-left:3px solid #1a7a1a;">
                          <div style="font-size:0.72rem;font-weight:700;color:#145214;
                                      text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.35rem;">
                            Actions correctives</div>
                          <div style="font-size:0.85rem;color:#333;line-height:1.55;">{a["sol"]}</div>
                        </div>
                      </div>
                        """, unsafe_allow_html=True)

                    st.markdown("</div>", unsafe_allow_html=True)


with tab4:
    try:
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.core.problem import Problem
        from pymoo.optimize import minimize as pymoo_min
        from pymoo.termination import get_termination
        pymoo_ok = True
    except ImportError:
        pymoo_ok = False

    st.markdown('<p class="section-title">Optimisation Multi-Objectif — Front de Pareto (NSGA-II)</p>', unsafe_allow_html=True)

    if not pymoo_ok:
        st.error("⚠️ pymoo non installé. Lancez : `pip install pymoo` puis redémarrez l'app.")
    else:
        st.divider()
        col_p, col_r = st.columns([1, 2])

        with col_p:
            st.markdown('<p class="section-title">Paramètres NSGA-II</p>', unsafe_allow_html=True)
            pop_size_p = st.slider("Population", 50, 500, 200, 50, key="pop_pareto")
            n_gen_p    = st.slider("Générations", 50, 500, 200, 50, key="gen_pareto")

            # Valeurs réelles liées automatiquement depuis source
            Q_HP_p = round((cadence / 100.0) * P["Q_HP_max"], 1)
            st.markdown(f"""
            <div style="background:white;border:1px solid #dde4ec;border-radius:10px;
                        padding:0.8rem 0.9rem;margin-top:0.5rem;margin-bottom:0.3rem;
                        box-shadow:0 1px 4px rgba(0,0,0,0.05);">
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;">
                <div style="background:#f4f8fc;border-radius:7px;padding:0.5rem 0.7rem;">
                  <div style="font-family:'Times New Roman',Times,serif;font-size:0.78rem;
                              color:#111;font-weight:700;">Cadence SAP</div>
                  <div style="font-family:'Times New Roman',Times,serif;font-weight:900;
                              color:#1a3a6e;font-size:1.1rem;">{cadence:.0f} %</div>
                </div>
                <div style="background:#f4f8fc;border-radius:7px;padding:0.5rem 0.7rem;">
                  <div style="font-family:'Times New Roman',Times,serif;font-size:0.78rem;
                              color:#111;font-weight:700;">Débit HP</div>
                  <div style="font-family:'Times New Roman',Times,serif;font-weight:900;
                              color:#1a3a6e;font-size:1.1rem;">{Q_HP_p} T/h</div>
                </div>
                <div style="background:#f1f8f1;border-radius:7px;padding:0.5rem 0.7rem;">
                  <div style="font-family:'Times New Roman',Times,serif;font-size:0.78rem;
                              color:#111;font-weight:700;">Demande MP</div>
                  <div style="font-family:'Times New Roman',Times,serif;font-weight:900;
                              color:#145214;font-size:1.1rem;">{D_MP:.1f} T/h</div>
                </div>
                <div style="background:#f1f8f1;border-radius:7px;padding:0.5rem 0.7rem;">
                  <div style="font-family:'Times New Roman',Times,serif;font-size:0.78rem;
                              color:#111;font-weight:700;">Demande BP</div>
                  <div style="font-family:'Times New Roman',Times,serif;font-weight:900;
                              color:#145214;font-size:1.1rem;">{D_BP:.1f} T/h</div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)
            run_p = st.button("Lancer l'optimisation Pareto", type="primary", use_container_width=True)

        with col_r:
            if run_p:
                class ParetoTurbine(Problem):
                    def __init__(self):
                        super().__init__(
                            n_var=5, n_obj=2, n_ieq_constr=7,
                            xl=np.array([100., 0., 0., 0., 0.]),
                            xu=np.array([Q_HP_p, 12., 57., 80., 120.])
                        )
                    def _evaluate(self, X, out, *args, **kwargs):
                        x1r, x4r, x5r, x2r, x3r = X[:,0], X[:,1], X[:,2], X[:,3], X[:,4]
                        # x4 forcé à 0 si x1 < 200 (contrainte physique turbine)
                        x4r_eff = np.where(x1r >= 200, x4r, 0.0)
                        P_mw = np.maximum(((x1r-x4r_eff-x5r)/3.5)+(x4r_eff/8)+(x5r/6.5), 0)
                        def_MP = np.maximum(D_MP - (x2r + x4r_eff + Q_JPH_recep), 0)
                        def_BP = np.maximum(D_BP - (x3r + x5r + Q_HRS), 0)
                        out['F'] = np.column_stack([-P_mw, def_MP + def_BP])
                        # x2_min = max(0, D_MP - Q_JPH) quand x1 < 200
                        x2_min_required = np.maximum(D_MP - Q_JPH_recep, 0)
                        x2_min_viol = np.where(x1r < 200,
                                               x2_min_required - x2r,   # x2 >= x2_min si x1<200
                                               np.zeros(len(x1r)))       # pas de contrainte si x1>=200
                        out['G'] = np.column_stack([
                            x1r + x2r + x3r - Q_HP_p - 2,
                            -(Q_HP_p - 2 - x1r - x2r - x3r),
                            -(x1r - x4r_eff - x5r),
                            -(P_mw - 10),
                            x4r + x5r - x1r,
                            # Contrainte: si x1<200 → x4 doit être ~0
                            np.where(x1r < 200, x4r - 0.5, np.zeros(len(x1r))),
                            # Contrainte: si x1<200 → x2 >= D_MP - Q_JPH
                            x2_min_viol,
                        ])

                with st.spinner(f"⏳ NSGA-II : {pop_size_p} individus × {n_gen_p} générations..."):
                    res_p = pymoo_min(ParetoTurbine(), NSGA2(pop_size=pop_size_p),
                                     get_termination("n_gen", n_gen_p), seed=42, verbose=False)

                if res_p.F is not None and len(res_p.F) > 0:
                    F = res_p.F; Xr = res_p.X
                    idx = np.argsort(F[:,0])
                    F, Xr = F[idx], Xr[idx]
                    mask = [True] + [not (abs(F[i,0]-F[i-1,0])<0.05 and abs(F[i,1]-F[i-1,1])<0.1) for i in range(1,len(F))]
                    F, Xr = F[mask], Xr[mask]

                    # ── Sélection Knee Points ──
                    # Normaliser le front [0,1]
                    F_norm = np.zeros_like(F)
                    for j in range(F.shape[1]):
                        fmin, fmax = F[:,j].min(), F[:,j].max()
                        F_norm[:,j] = (F[:,j] - fmin) / (fmax - fmin + 1e-10)

                    # Point idéal = (0,0) en normalisé
                    # Knee = point le plus éloigné de la droite reliant les extrêmes
                    # Méthode: distance à la droite A→B du front
                    A = F_norm[0]   # extrême max P_elec
                    B = F_norm[-1]  # extrême min déficit
                    AB = B - A
                    AB_norm = AB / (np.linalg.norm(AB) + 1e-10)

                    distances = []
                    for i in range(len(F_norm)):
                        AP = F_norm[i] - A
                        proj = np.dot(AP, AB_norm)
                        perp = AP - proj * AB_norm
                        distances.append(np.linalg.norm(perp))

                    distances = np.array(distances)

                    # Garder 15 points: knee principal + points uniformes autour
                    n_keep = min(14, len(F))
                    knee_idx = np.argmax(distances)  # le vrai knee point

                    # Points uniformes sur le front (espacés)
                    uniform_idx = np.linspace(0, len(F)-1, n_keep, dtype=int)

                    # Assurer que le knee point est inclus
                    all_idx = np.unique(np.append(uniform_idx, knee_idx))
                    all_idx = np.sort(all_idx)

                    F_sel  = F[all_idx]
                    Xr_sel = Xr[all_idx]
                    knee_pos = np.where(all_idx == knee_idx)[0][0]  # position dans F_sel

                    st.session_state["pareto_F"]    = F_sel
                    st.session_state["pareto_X"]    = Xr_sel
                    st.session_state["pareto_knee"] = knee_pos
                    st.success(f"✅ Front de Pareto : **{len(F_sel)} solutions** (knee point sélectionné)")
                else:
                    st.error("❌ Aucune solution faisable — relaxez les contraintes")

            if "pareto_F" in st.session_state:
                F  = st.session_state["pareto_F"]
                Xr = st.session_state["pareto_X"]
                P_vals  = -F[:,0]
                def_vals =  F[:,1]

                knee_pos = st.session_state.get("pareto_knee", 0)

                fig_par = go.Figure()
                fig_par.add_trace(go.Scatter(
                    x=def_vals, y=P_vals, mode="markers+lines",
                    marker=dict(size=8, color=P_vals, colorscale="RdYlGn",
                                colorbar=dict(title="MW"), showscale=True),
                    line=dict(color="rgba(31,78,121,0.25)", width=1),
                    text=[f"Sol {i+1}<br>P={p:.2f} MW<br>Déficit={d:.1f} T/h"
                          for i,(p,d) in enumerate(zip(P_vals,def_vals))],
                    hovertemplate="%{text}<extra></extra>", name="Front de Pareto"
                ))
                # Knee point — compromis optimal
                fig_par.add_trace(go.Scatter(
                    x=[def_vals[knee_pos]], y=[P_vals[knee_pos]],
                    mode="markers",
                    marker=dict(size=16, color="orange", symbol="diamond",
                                line=dict(color="black", width=2)),
                    name="🔶 Knee Point (compromis optimal)",
                    hovertemplate=f"Knee Point<br>P={P_vals[knee_pos]:.2f} MW<br>Déficit={def_vals[knee_pos]:.1f} T/h<extra></extra>"
                ))
                P_act   = calc_puissance(x1, x4, x5)
                def_act = max(0, D_MP-(x2+x4+Q_JPH_recep)) + max(0, D_BP-(x3+x5+Q_HRS))
                fig_par.add_trace(go.Scatter(
                    x=[def_act], y=[P_act], mode="markers",
                    marker=dict(size=14, color="red", symbol="star"),
                    name="⭐ Point actuel",
                    hovertemplate=f"Actuel<br>P={P_act:.2f} MW<br>Déficit={def_act:.1f} T/h<extra></extra>"
                ))
                fig_par.update_layout(
                    title=dict(text="Front de Pareto — NSGA-II",
                               font=dict(family="Times New Roman", size=14, color="#1a2e4a")),
                    xaxis_title="Déficit clients (T/h)  →  Minimiser",
                    yaxis_title="P_elec (MW)  ↑  Maximiser",
                    font=dict(family="Times New Roman", size=12),
                    xaxis=dict(title_font=dict(family="Times New Roman", size=12),
                               tickfont=dict(family="Times New Roman", size=11)),
                    yaxis=dict(title_font=dict(family="Times New Roman", size=12),
                               tickfont=dict(family="Times New Roman", size=11)),
                    height=420, margin=dict(l=0,r=0,t=45,b=0),
                    hovermode="closest",
                    legend=dict(orientation="h", y=-0.2, x=0,
                                font=dict(family="Times New Roman", size=11)),
                    plot_bgcolor="white",
                    paper_bgcolor="white"
                )
                st.plotly_chart(fig_par, use_container_width=True)

        if "pareto_F" in st.session_state:
            F   = st.session_state["pareto_F"]
            Xr  = st.session_state["pareto_X"]
            P_vals   = -F[:,0]
            def_vals =  F[:,1]
            knee_pos = st.session_state.get("pareto_knee", 0)

            # ── Indices des 3 solutions ──
            idx_maxP = np.argmax(P_vals)
            idx_minD = np.argmin(def_vals)
            idx_knee = knee_pos

            # ── Situation actuelle ──
            P_act   = calc_puissance(x1, x4, x5)
            def_act = max(0, D_MP-(x2+x4+Q_JPH_recep)) + max(0, D_BP-(x3+x5+Q_HRS))

            # ── Variables de décision pour chaque solution ──
            def sol_vars(idx):
                x1r,x4r,x5r,x2r,x3r = Xr[idx]
                x6r = max(x1r-x4r-x5r, 0)
                return dict(x1=round(x1r,1), x2=round(x2r,1), x3=round(x3r,1),
                            x4=round(x4r,1), x5=round(x5r,1), x6=round(x6r,1),
                            P=round(P_vals[idx],2), D=round(def_vals[idx],1))

            s1, s2, s3 = sol_vars(idx_maxP), sol_vars(idx_knee), sol_vars(idx_minD)
            sa = dict(x1=round(x1,1), x2=round(x2,1), x3=round(x3,1),
                      x4=round(x4,1), x5=round(x5,1),
                      x6=round(max(x1-x4-x5,0),1),
                      P=round(P_act,2), D=round(def_act,1))

            # ── Gains vs actuel ──
            g1P = s1["P"]-sa["P"]; g1D = sa["D"]-s1["D"]
            g2P = s2["P"]-sa["P"]; g2D = sa["D"]-s2["D"]
            g3P = s3["P"]-sa["P"]; g3D = sa["D"]-s3["D"]

            def gain_str(val, unit, positive_good=True):
                if abs(val) < 0.05: return f'<span style="color:#888;">≈ 0 {unit}</span>'
                good = (val > 0) == positive_good
                clr  = "#1a7a1a" if good else "#e53935"
                sign = "+" if val > 0 else ""
                return f'<span style="color:{clr};font-weight:700;">{sign}{val:.1f} {unit}</span>'

            st.divider()
            st.markdown('<p class="section-title">Solutions Optimales — Front de Pareto</p>',
                        unsafe_allow_html=True)

            # ── 3 KPI Cards ──
            c1, c2, c3 = st.columns(3)
            cards = [
                (c1, "Max Puissance", "#1a7a1a", "#e8f5e9", s1,
                 g1P, g1D, "Priorité production électrique"),
                (c2, "Compromis Optimal", "#f9a825", "#fff8e1", s2,
                 g2P, g2D, "Knee point — équilibre puissance / clients"),
                (c3, "Min Déficit Clients", "#e53935", "#fdecea", s3,
                 g3P, g3D, "Satisfaction maximale MP & BP"),
            ]
            for col, titre, clr, bg, s, gP, gD, desc in cards:
                with col:
                    st.markdown(f"""
                    <div style="background:white;border:1px solid #e0e7ef;border-radius:12px;
                                box-shadow:0 3px 10px rgba(0,0,0,0.07);overflow:hidden;
                                border-top:4px solid {clr};">
                      <!-- Titre centré + cercle -->
                      <div style="text-align:center;padding:1rem 1rem 0.75rem 1rem;">
                        <div style="display:inline-flex;align-items:center;justify-content:center;
                                    width:42px;height:42px;border-radius:50%;
                                    background:{clr}18;border:2.5px solid {clr};
                                    margin-bottom:0.45rem;">
                          <div style="width:15px;height:15px;border-radius:50%;
                                      background:{clr};"></div>
                        </div>
                        <div style="font-family:'Times New Roman',Times,serif;font-size:1.05rem;
                                    font-weight:700;color:#111;">{titre}</div>
                      </div>
                      <!-- Corps -->
                      <div style="padding:0 1rem 1rem 1rem;">
                        <!-- P_elec -->
                        <div style="background:#f8fafc;border-radius:8px;padding:0.8rem 0.9rem;
                                    margin-bottom:0.5rem;border-left:4px solid {clr};">
                          <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                                      color:#111;font-weight:700;margin-bottom:0.3rem;">
                            Puissance Électrique</div>
                          <div style="font-family:'Times New Roman',Times,serif;font-size:1.7rem;
                                      font-weight:900;color:{clr};line-height:1.1;">
                            {s["P"]} <span style="font-family:'Times New Roman',Times,serif;
                                           font-size:0.92rem;font-weight:700;color:#111;">MW</span>
                          </div>
                          <div style="font-family:'Times New Roman',Times,serif;font-size:0.8rem;
                                      color:#111;margin-top:0.25rem;font-weight:600;">
                            vs actuel {sa["P"]} MW → {gain_str(gP, "MW", True)}
                          </div>
                        </div>
                        <!-- Déficit -->
                        <div style="background:#f8fafc;border-radius:8px;padding:0.8rem 0.9rem;
                                    border-left:4px solid {clr};">
                          <div style="font-family:'Times New Roman',Times,serif;font-size:0.9rem;
                                      color:#111;font-weight:700;margin-bottom:0.3rem;">
                            Déficit Clients</div>
                          <div style="font-family:'Times New Roman',Times,serif;font-size:1.7rem;
                                      font-weight:900;color:{clr};line-height:1.1;">
                            {s["D"]} <span style="font-family:'Times New Roman',Times,serif;
                                           font-size:0.92rem;font-weight:700;color:#111;">T/h</span>
                          </div>
                          <div style="font-family:'Times New Roman',Times,serif;font-size:0.8rem;
                                      color:#111;margin-top:0.25rem;font-weight:600;">
                            vs actuel {sa["D"]} T/h → {gain_str(gD, "T/h", True)}
                          </div>
                        </div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

            # ── Tableau variables de décision ──
            st.markdown('<p class="section-title" style="margin-top:1.5rem;">Variables de Décision — Configuration optimale par solution</p>',
                        unsafe_allow_html=True)

            def var_cell(val, unit="T/h", highlight=False, clr="#1a3a6e"):
                bg = ("background:"+clr+"11;") if highlight else ""
                fw = "700" if highlight else "400"
                co = clr if highlight else "#333"
                style = "font-family:Times New Roman,Times,serif;text-align:center;padding:0.55rem 0.7rem;"
                style += bg + "font-weight:" + fw + ";color:" + co + ";"
                td = '<td style="' + style + '">' + str(val) + ' <span style="font-size:0.75rem;color:#888;">' + unit + '</span></td>'
                return td

            rows_html = ""
            # ── Ligne point actuel ──
            rows_html += f"""
                <tr style="background:#eef3f9;border-bottom:2px solid #2e6da4;">
                  <td style="font-family:'Times New Roman',Times,serif;padding:0.55rem 0.8rem;
                              border-left:4px solid #4a6fa5;font-weight:700;color:#4a6fa5;">Point actuel</td>
                  {var_cell(sa["x1"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["x2"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["x3"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["x4"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["x5"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["x6"],"T/h",False,"#4a6fa5")}
                  {var_cell(sa["P"], "MW", False,"#4a6fa5")}
                  {var_cell(sa["D"], "T/h",False,"#4a6fa5")}
                </tr>"""
            # ── Toutes les solutions Pareto ──
            for i in range(len(F)):
                x1r,x4r,x5r,x2r,x3r = Xr[i]
                x6r = round(max(x1r-x4r-x5r,0),1)
                pi  = round(P_vals[i],2)
                di  = round(def_vals[i],1)
                is_maxP = (i == idx_maxP)
                is_knee = (i == idx_knee)
                is_minD = (i == idx_minD)
                if is_maxP:
                    rc, rb, badge = "#1a7a1a","#e8f5e9"," ★ Max P"
                elif is_knee:
                    rc, rb, badge = "#f9a825","#fff8e1"," ◆ Comp."
                elif is_minD:
                    rc, rb, badge = "#e53935","#fdecea"," ▼ Min D"
                else:
                    rc, rb, badge = "#2e6da4","#f8fafc",""
                fw_td = "font-weight:700;" if (is_maxP or is_knee or is_minD) else ""
                lbl = f"S{i+1}{badge}"
                rows_html += f"""
                <tr style="background:{rb};">
                  <td style="font-family:'Times New Roman',Times,serif;padding:0.45rem 0.8rem;
                              border-left:4px solid {rc};{fw_td}color:{rc};">{lbl}</td>
                  {var_cell(round(x1r,1),"T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(round(x2r,1),"T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(round(x3r,1),"T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(round(x4r,1),"T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(round(x5r,1),"T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(x6r,       "T/h",is_maxP or is_knee or is_minD,rc)}
                  {var_cell(pi,        "MW", is_maxP or is_knee or is_minD,rc)}
                  {var_cell(di,        "T/h",is_maxP or is_knee or is_minD,rc)}
                </tr>"""

            st.markdown(f"""
            <div style="overflow-x:auto;border-radius:10px;border:1px solid #dde4ec;
                        box-shadow:0 2px 8px rgba(0,0,0,0.06);margin-top:0.5rem;">
            <table style="width:100%;border-collapse:collapse;font-family:'Times New Roman',Times,serif;">
              <thead>
                <tr style="background:#2e6da4;color:white;">
                  <th style="padding:0.65rem 0.8rem;text-align:left;font-size:0.82rem;
                              font-weight:700;letter-spacing:0.03em;">Solution</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x1 Turbine</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x2 Bypass MP</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x3 Bypass BP</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x4 Sout. MP</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x5 Sout. BP</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">x6 Condenseur</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">P_elec (MW)</th>
                  <th style="padding:0.65rem 0.7rem;text-align:center;font-size:0.82rem;">Déficit (T/h)</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            </div>
            """, unsafe_allow_html=True)

# ══════════════════════════════════════════════
# TAB 5 — SCHÉMA P&ID
# ══════════════════════════════════════════════


with tab5:
    # ── Local : lecture SQLite (persistant) | Cloud : session_state (session courante) ──
    hist = _load_hist()
    st.session_state["hist_diag"] = hist

    if not hist:
        st.markdown("""
        <div style="background:white;border:1px solid #dde4ec;border-radius:12px;
                    padding:2.5rem;text-align:center;font-family:'Times New Roman',Times,serif;
                    color:#aaa;font-style:italic;margin-top:1rem;
                    box-shadow:0 2px 8px rgba(0,0,0,0.04);">
          <div style="font-size:2rem;margin-bottom:0.5rem;">📋</div>
          Aucun enregistrement — lancez un diagnostic depuis l'onglet Diagnostic
        </div>""", unsafe_allow_html=True)
    else:
        # ── Paramètres à afficher (clés mesures disponibles) ──
        # On détermine les colonnes paramètres à partir du premier enregistrement avec mesures
        PARAM_KEYS = []
        for h in hist:
            mes = h.get("mesures", {})
            if mes:
                PARAM_KEYS = [k for k in mes.keys() if k in TAGS]
                break
        if not PARAM_KEYS:
            PARAM_KEYS = [k for k in TAGS.keys()]

        # ── Construire header ──
        header_params = ""
        for p in PARAM_KEYS:
            info = TAGS.get(p, {})
            nom_court = info.get("nom", p)
            # Tronquer si trop long
            if len(nom_court) > 18:
                nom_court = nom_court[:16] + "…"
            unite = info.get("unite", "")
            _unite_sub = (f'<br><span style="font-weight:400;font-size:0.62rem;opacity:0.8;">({unite})</span>' if unite else "")
            header_params += (
                f'<th style="padding:0.5rem 0.55rem;text-align:center;font-size:0.69rem;'
                f'white-space:nowrap;font-weight:700;letter-spacing:0.02em;'
                f'background:#1a3a6e;border-left:1px solid rgba(255,255,255,0.1);">'
                f'{nom_court}{_unite_sub}'
                f'</th>'
            )

        # ── Construire lignes ──
        rows_html = ""
        for i, h in enumerate(hist):
            bg_row = "#fff" if i % 2 == 0 else "#f8fafc"
            mesures = h.get("mesures", {})

            param_cells = ""
            for p in PARAM_KEYS:
                info     = TAGS.get(p, {})
                val      = mesures.get(p)
                nominal  = info.get("nominal")
                unite    = info.get("unite", "")

                # Couleur selon écart vs nominal
                dot_color = "#bdbdbd"
                cell_bg   = ""
                if val is not None and nominal is not None:
                    try:
                        if nominal == 0:
                            # nominal=0 : tolérance absolue (bypass, ATM, etc.)
                            ec_abs = abs(float(val))
                            if ec_abs > 5:
                                dot_color = "#e53935"
                                cell_bg   = "background:#fff5f5;"
                            elif ec_abs > 0:
                                dot_color = "#f9a825"
                                cell_bg   = "background:#fffde7;"
                            else:
                                dot_color = "#43a047"
                        else:
                            ec = abs((float(val) - float(nominal)) / float(nominal) * 100)
                            if ec >= 10:
                                dot_color = "#e53935"
                                cell_bg   = "background:#fff5f5;"
                            elif ec >= 5:
                                dot_color = "#f9a825"
                                cell_bg   = "background:#fffde7;"
                            else:
                                dot_color = "#43a047"
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass

                if val is not None:
                    try:
                        val_disp = f"{float(val):.1f}"
                    except (TypeError, ValueError):
                        val_disp = str(val)
                else:
                    val_disp = "—"

                param_cells += (
                    f'<td style="padding:0.38rem 0.5rem;text-align:center;'
                    f'font-family:"Times New Roman",serif;font-size:0.74rem;{cell_bg}'
                    f'border-left:1px solid #f0f2f5;">'
                    f'<span style="display:inline-block;width:5px;height:5px;border-radius:50%;'
                    f'background:{dot_color};margin-right:3px;vertical-align:middle;"></span>'
                    f'<span style="font-weight:600;color:#1a1a2e;">{val_disp}</span>'
                    f'</td>'
                )

            # Formatage date/heure
            date_str = h.get("date", "")
            heure_str = h.get("heure", "")
            cadence_str = h.get("cadence", "—")
            x1_str = f"{h.get('x1', '—')} T/h"

            rows_html += f"""
            <tr style="background:{bg_row};border-bottom:1px solid #eef0f4;transition:background 0.15s;"
                onmouseover="this.style.background='#edf3fb'" onmouseout="this.style.background='{bg_row}'">
              <td style="padding:0.42rem 0.75rem;font-family:'Times New Roman',serif;font-size:0.76rem;
                          color:#1a3a6e;white-space:nowrap;font-weight:700;border-right:2px solid #dde4ec;">
                <div style="font-size:0.78rem;font-weight:800;">{date_str}</div>
                <div style="font-size:0.68rem;color:#888;font-weight:500;margin-top:1px;">{heure_str}</div>
              </td>
              <td style="padding:0.42rem 0.6rem;text-align:center;font-family:'Times New Roman',serif;
                          font-size:0.74rem;color:#444;font-weight:600;">{cadence_str}</td>
              <td style="padding:0.42rem 0.6rem;text-align:center;font-family:'Times New Roman',serif;
                          font-size:0.74rem;color:#2e6da4;font-weight:700;">{x1_str}</td>
              {param_cells}
            </tr>"""

        st.markdown(f"""
        <div style="overflow-x:auto;border-radius:12px;border:1px solid #dde4ec;
                    box-shadow:0 3px 12px rgba(0,0,0,0.07);margin-top:0.2rem;">
        <table style="width:100%;border-collapse:collapse;font-family:'Times New Roman',Times,serif;">
          <thead>
            <tr style="background:#2e6da4;color:white;">
              <th style="padding:0.55rem 0.75rem;text-align:left;font-size:0.75rem;white-space:nowrap;
                          font-weight:700;letter-spacing:0.03em;border-right:2px solid rgba(255,255,255,0.2);">
                Date / Heure</th>
              <th style="padding:0.55rem 0.6rem;text-align:center;font-size:0.75rem;font-weight:700;">Cadence</th>
              <th style="padding:0.55rem 0.6rem;text-align:center;font-size:0.75rem;font-weight:700;">x1 Turbine</th>
              {header_params}
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        </div>
        """, unsafe_allow_html=True)

        # ── Bouton effacer historique ──
        st.markdown("<div style='margin-top:1rem;'></div>", unsafe_allow_html=True)
        col_eff1, col_eff2 = st.columns([4, 1])
        with col_eff2:
            if st.button("🗑 Effacer l'historique", type="secondary", use_container_width=True):
                st.session_state["hist_diag"] = []
                _clear_hist()
                st.rerun()


# ─────────────────────────────────────────────
#  FOOTER
# ─────────────────────────────────────────────
st.divider()
st.markdown("""
<div style="text-align:center;color:#888;font-size:0.8rem;padding:0.5rem">
    EMS — Centrale Thermique OCP | UNITE 515A<br>
    Développé dans le cadre du PFE — Master Électronique, Matériaux et Énergies
</div>
""", unsafe_allow_html=True)
