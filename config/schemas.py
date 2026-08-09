"""
config/schemas.py
Schema definitions (hardcoded + YAML-override loader).
"""

import os
from config.settings import CONFIG_DIR

# ── Simple YAML parser (no pyyaml dependency) ─────────────────────────────────
def _parse_yaml_simple(text: str) -> dict:
    def _cast(v: str):
        v = v.strip()
        if not v or v.lower() in ("null", "~", ""):
            return None
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        try:
            return int(v)
        except Exception:
            pass
        try:
            return float(v)
        except Exception:
            pass
        return v.strip('"').strip("'")

    lines = text.splitlines()
    root = {}
    stack = [(0, root)]
    cur_key = None
    for raw in lines:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line   = raw.strip()
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            val = line[2:].strip()
            if cur_key and isinstance(parent, dict):
                if not isinstance(parent.get(cur_key), list):
                    parent[cur_key] = []
                parent[cur_key].append(_cast(val))
        elif ":" in line:
            parts = line.split(":", 1)
            key   = parts[0].strip().strip('"').strip("'")
            val   = parts[1].strip() if len(parts) > 1 else ""
            if " #" in val:
                val = val[: val.index(" #")].strip()
            cur_key = key
            if val:
                parent[key] = _cast(val)
            else:
                parent[key] = {}
                stack.append((indent + 2, parent[key]))
    return root


def load_schema_config(schema_filename: str) -> dict | None:
    path = os.path.join(CONFIG_DIR, schema_filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _parse_yaml_simple(f.read())
    except Exception:
        return None


def _merge_schema_from_config(hardcoded: dict, cfg: dict | None) -> dict:
    if not cfg:
        return hardcoded
    merged       = dict(hardcoded)
    schema_block = cfg.get("schema", {})
    for k in ("version", "description"):
        if schema_block.get(k):
            merged[k] = schema_block[k]
    if cfg.get("required_fields"):
        rf = cfg["required_fields"]
        if isinstance(rf, dict):
            rf = list(rf.keys())
        if isinstance(rf, list):
            merged["required_fields"] = [str(f) for f in rf if f]
    if cfg.get("accepted_fields"):
        af = cfg["accepted_fields"]
        if isinstance(af, dict):
            af = list(af.keys())
        if isinstance(af, list):
            merged["accepted_fields"] = [str(f) for f in af if f]
    if cfg.get("field_aliases") and isinstance(cfg["field_aliases"], dict):
        aliases = {}
        for field, vals in cfg["field_aliases"].items():
            if isinstance(vals, list):
                aliases[field] = [str(v) for v in vals if v]
            elif isinstance(vals, str):
                aliases[field] = [vals]
        if aliases:
            merged["field_aliases"] = aliases
    conf_block = cfg.get("confidence", {})
    if isinstance(conf_block, dict):
        if conf_block.get("field_thresholds") and isinstance(conf_block["field_thresholds"], dict):
            merged["field_thresholds"] = {
                k: int(v) for k, v in conf_block["field_thresholds"].items() if v is not None
            }
        if conf_block.get("global_threshold") is not None:
            merged["config_threshold"] = int(conf_block["global_threshold"])
    if cfg.get("export") and isinstance(cfg["export"], dict):
        merged["export_config"] = cfg["export"]
    return merged


# ── Hardcoded schema definitions ──────────────────────────────────────────────
_HARDCODED_SCHEMAS: dict = {
    "Guidewire": {
        "color": "#4f9cf9", "icon": "🔵", "css_cls": "guide",
        "version": "ClaimCenter 10.x",
        "description": "Guidewire ClaimCenter 10.x compatible format",
        "date_format": "YYYY-MM-DD",
        "amount_format": "decimal",
        "status_values": ["open", "closed", "pending", "reopened", "denied", "submitted", "draft"],
        "required_fields": [
            "Claim Number", "Claimant Name", "Loss Date",
            "Total Incurred", "Total Paid", "Reserve",
            "Status", "Line of Business", "Policy Number",
        ],
        "accepted_fields": [
            "Claim Number", "Claimant Name", "Loss Date", "Date Reported",
            "Total Incurred", "Total Paid", "Reserve", "Indemnity Paid",
            "Medical Paid", "Expense Paid", "Status", "Line of Business",
            "Policy Number", "Policy Period Start", "Policy Period End",
            "Carrier", "Insured Name", "Description of Loss",
            "Cause of Loss", "Litigation Flag", "Adjuster Name",
            "Adjuster Phone", "Branch Code", "Department Code",
            "Coverage Type", "Deductible", "Subrogation Amount",
            "Recovery Amount", "Open/Closed", "Reopen Date",
            "Last Activity Date", "Days Lost", "State", "Notes",
            "Job Title", "Body Part", "Vehicle ID", "At Fault",
            "Building Damage", "Contents Damage", "Business Interruption Loss",
            "Net Paid", "Services Involved", "Location",
            # NEW — canonical fields produced by parsing.py's title-KV pass
            # (see extract_sheet_title_kvs()). Without these, the values are
            # extracted correctly but map_claim_to_schema() never asks for
            # them, so they're silently dropped on export.
            "Contract Number", "Underwriting Period Start",
            "Underwriting Period End", "Program Year",
        ],
        "field_aliases": {
            "Claim Number":      ["claim_id","claim number","claim no","claim#","claimid","claim ref","claim #","file no","file number","file#","ref no","ref number","reference no","reference number","loss ref"],
            # FIX: removed "insured name" — it belongs exclusively to Insured Name below.
            # Added "claimant" variants only; "policy holder" maps to Insured Name.
            "Claimant Name":     ["claimant name","claimant","name","injured party","employee name","driver name","injured worker","plaintiff","first party","tort claimant"],
            "Loss Date":         ["date of loss","loss date","loss dt","date of accident","incident date","date of injury","injury date","date of incident","occurrence date","event date","accident dt","doi"],
            "Date Reported":     ["date reported","reported date","report date","date opened","open date","intake date"],
            "Total Incurred":    ["total incurred","incurred","total incurred amount","total exposure","gross incurred","net incurred"],
            "Total Paid":        ["total paid","amount paid","paid amount","net paid","gross paid","total disbursed","total payments"],
            "Reserve":           ["reserve","outstanding reserve","case reserve","total reserve","open reserve","unpaid reserve","ibnr reserve"],
            "Indemnity Paid":    ["indemnity paid","indemnity","wage loss paid","ttd paid","bi paid","lost wages","disability paid","indemnity payments"],
            "Medical Paid":      ["medical paid","medical","med paid","medical payments","med bills","healthcare paid"],
            "Expense Paid":      ["expense paid","expense","legal expense","defense costs","alae","dlae","allocated expense","litigation costs"],
            "Status":            ["status","claim status","open/closed","file status","current status"],
            "Line of Business":  ["line of business","lob","coverage line","policy type","coverage type","type of insurance","insurance line"],
            "Policy Number":     ["policy number","policy no","policy#","policy id","policy :","policy #","pol no","pol num","pol #","policy_number"],
            # FIX: added "policy holder name", "policy holder", "policyholder name" so that
            # column is captured here and NOT also matched to Claimant Name.
            "Insured Name":      ["insured name","insured","employer name","named insured","policyholder","account name","policy holder name","policy holder","policyholder name","policy_holder","policy_holder_name"],
            "Description of Loss": ["description of loss","loss description","description","narrative","nature of injury","nature of claim","type of loss","cause of loss","incident description","accident description","loss narrative","claim description","summary"],
            "Cause of Loss":     ["cause of loss","cause","type of loss","peril","nature of injury","nature of claim","accident type","loss type","col"],
            "Adjuster Name":     ["adjuster name","adjuster","examiner","claim examiner","handler","claim handler","assigned adjuster"],
            "Coverage Type":     ["coverage","coverage type","type of coverage"],
            "Deductible":        ["deductible","deductible amount","self insured retention","sir","deductible applied"],
            "Days Lost":         ["days lost","days of disability","lost days","disability days","days missed","calendar days lost"],
            "Job Title":         ["job title","occupation","position","employee title","job class","employee occupation"],
            "Body Part":         ["body part","body part injured","part of body","injured body part","injury location"],
            "Vehicle ID":        ["vehicle id","vehicle","unit number","vin","vehicle number","unit no"],
            "At Fault":          ["at fault","fault","liable","at-fault","liability","fault determination"],
            "Building Damage":   ["building damage","structure damage","building loss","building amount"],
            "Contents Damage":   ["contents damage","contents loss","stock loss","contents amount"],
            "Business Interruption Loss": ["bi loss","business interruption","business income loss","time element","loss of use"],
            "Net Paid":          ["net paid","pd paid","property damage paid","net claim payment","net disbursed"],
            "Services Involved": ["services involved","professional services","service type","services rendered"],
            "Location":          ["location","property location","site","premises","loss location","risk location","address"],
            # NEW — aliases for the newly-accepted canonical fields
            "Contract Number":   ["contract number","contract no","contract #","contract","contract ref"],
            "Underwriting Period Start": ["underwriting period start","underwriting start","underwriting period from"],
            "Underwriting Period End":   ["underwriting period end","underwriting end","underwriting period to"],
            "Program Year":      ["program year","policy year","treaty year"],
        },
    },
    "Duck Creek": {
        "color": "#f5c842", "icon": "🟡", "css_cls": "duck",
        "version": "Claims 6.x",
        "description": "Duck Creek Claims 6.x transaction format",
        "date_format": "MM/DD/YYYY",
        "amount_format": "decimal",
        "status_values": ["Open", "Closed", "Pending", "Reopen", "Denied", "Settled"],
        "required_fields": [
            "Claim Id", "Claimant Name", "Loss Date",
            "Total Incurred", "Total Paid", "Reserve",
            "Policy Number", "Claim Status",
        ],
        "accepted_fields": [
            "Claim Id", "Transaction Id", "Claimant Name", "Loss Date",
            "Date Reported", "Total Incurred", "Total Paid", "Reserve",
            "Indemnity Paid", "Medical Paid", "Expense Paid",
            "Policy Number", "Policy Effective Date", "Policy Expiry Date",
            "Claim Status", "Cause of Loss", "Description of Loss",
            "Insured Name", "Carrier Name", "Line of Business",
            "Adjuster Id", "Adjuster Name", "Office Code",
            "Jurisdiction", "State Code", "Deductible Amount",
            "Subrogation Flag", "Recovery Amount", "Litigation Flag",
            "Date Closed", "Date Reopened", "Last Updated Date", "Days Lost",
            "Notes", "Job Title", "Body Part", "Vehicle ID", "At Fault",
            "Building Damage", "Contents Damage", "Business Interruption Loss",
            "Net Paid", "Services Involved", "Property Location", "Coverage",
            # NEW — canonical fields produced by parsing.py's title-KV pass.
            # "Carrier" (not "Carrier Name") is included separately because the
            # title_fields dict from extract_sheet_title_kvs() keys on the
            # canonical name "Carrier" exactly; map_claim_to_schema() only
            # picks up a title field when the schema_field string matches that
            # key exactly, so an alias alone would not have been enough.
            "Contract Number", "Underwriting Period Start",
            "Underwriting Period End", "Program Year", "Carrier",
        ],
        "field_aliases": {
            "Claim Id":          ["claim_id","claim number","claim no","claim#","claimid","claim ref","claim #","file no","file number","ref no","reference no","loss ref"],
            # FIX: removed "insured name" — it belongs exclusively to Insured Name below.
            "Claimant Name":     ["claimant name","claimant","name","injured party","employee name","driver name","injured worker","plaintiff","first party"],
            "Loss Date":         ["date of loss","loss date","loss dt","date of accident","incident date","date of injury","injury date","date of incident","occurrence date","event date","doi"],
            "Date Reported":     ["date reported","reported date","report date","date opened","open date","intake date"],
            "Total Incurred":    ["total incurred","incurred","total incurred amount","total exposure","gross incurred"],
            "Total Paid":        ["total paid","amount paid","paid amount","net paid","gross paid","total disbursed"],
            "Reserve":           ["reserve","outstanding reserve","case reserve","total reserve","unpaid reserve"],
            "Indemnity Paid":    ["indemnity paid","indemnity","wage loss paid","ttd paid","bi paid","lost wages","disability paid"],
            "Medical Paid":      ["medical paid","medical","med paid","medical payments","med bills"],
            "Expense Paid":      ["expense paid","expense","legal expense","defense costs","alae","dlae"],
            "Claim Status":      ["status","claim status","open/closed","file status","current status"],
            "Line of Business":  ["line of business","lob","coverage line","policy type","type of insurance"],
            "Policy Number":     ["policy number","policy no","policy#","policy id","policy :","policy #","pol no","pol num"],
            # FIX: added "policy holder name", "policy holder", "policyholder name"
            "Insured Name":      ["insured name","insured","employer name","named insured","policyholder","account name","policy holder name","policy holder","policyholder name","policy_holder","policy_holder_name"],
            "Description of Loss": ["description of loss","loss description","description","narrative","nature of injury","nature of claim","type of loss","incident description","loss narrative","claim description","summary"],
            "Cause of Loss":     ["cause of loss","cause","type of loss","peril","nature of injury","col","accident type"],
            "Carrier Name":      ["carrier","carrier name","insurance company","insurer","underwriter"],
            "Deductible Amount": ["deductible","deductible amount","sir","self insured retention"],
            "Jurisdiction":      ["state","state code","jurisdiction","venue state"],
            "Days Lost":         ["days lost","days of disability","lost days","disability days","days missed"],
            "Job Title":         ["job title","occupation","position","employee title","job class"],
            "Body Part":         ["body part","body part injured","part of body","injured body part"],
            "Vehicle ID":        ["vehicle id","vehicle","unit number","vin","vehicle number"],
            "At Fault":          ["at fault","fault","liable","at-fault","liability"],
            "Building Damage":   ["building damage","structure damage","building loss"],
            "Contents Damage":   ["contents damage","contents loss","stock loss"],
            "Business Interruption Loss": ["bi loss","business interruption","business income loss","time element"],
            "Net Paid":          ["net paid","pd paid","property damage paid","net claim payment"],
            "Services Involved": ["services involved","professional services","service type"],
            "Property Location": ["location","property location","site","premises","loss location","risk location","address"],
            "Coverage":          ["coverage","coverage type","type of coverage","subject to $50k sir","within policy limits","coverage under review"],
            # NEW — aliases for the newly-accepted canonical fields
            "Contract Number":   ["contract number","contract no","contract #","contract","contract ref"],
            "Underwriting Period Start": ["underwriting period start","underwriting start","underwriting period from"],
            "Underwriting Period End":   ["underwriting period end","underwriting end","underwriting period to"],
            "Program Year":      ["program year","policy year","treaty year"],
            "Carrier":           ["carrier","carrier name","underwriter"],
        },
    },
}

# ── Build final SCHEMAS with YAML overrides ───────────────────────────────────
_CONFIG_LOAD_STATUS: dict = {}

def _load_all_configs(hardcoded: dict) -> dict:
    filemap = {"Guidewire": "guidewire.yaml", "Duck Creek": "duck_creek.yaml"}
    result  = {}
    for name, schema in hardcoded.items():
        fname = filemap.get(name)
        cfg   = load_schema_config(fname) if fname else None
        result[name] = _merge_schema_from_config(schema, cfg)
        _CONFIG_LOAD_STATUS[name] = {
            "file":   fname,
            "loaded": cfg is not None,
            "path":   os.path.join(CONFIG_DIR, fname) if fname else "",
        }
    return result


SCHEMAS: dict = _load_all_configs(_HARDCODED_SCHEMAS)
