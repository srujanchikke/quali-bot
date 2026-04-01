"""
flow_query.py  —  unified query layer (replaces query.py)
==========================================================
Handles two input shapes:

  Shape A — connector + flow name  (old run_pipeline.py style)
    q.check_connector_flow("Stripe", "Overcapture")

  Shape B — flow_id JSON           (new run_flow_pipeline.py style)
    q.check_coverage(flow_json, connector="Stripe")

query.py is deprecated — import from flow_query instead.
"""
from __future__ import annotations
import re, json, os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from neo4j import GraphDatabase

CONNECTOR_NAME_MAP: dict[str, str] = {
    "aci":"Aci","adyen":"Adyen","airwallex":"Airwallex","archipel":"Archipel",
    "authipay":"Authipay","authorizedotnet":"Authorizedotnet","bambora":"Bambora",
    "bamboraapac":"Bamboraapac","bankofamerica":"BankOfAmerica","barclaycard":"Barclaycard",
    "billwerk":"Billwerk","bluesnap":"Bluesnap","braintree":"Braintree","calida":"Calida",
    "cashtocode":"Cashtocode","celero":"Celero","checkbook":"Checkbook","checkout":"Checkout",
    "cryptopay":"Cryptopay","cybersource":"Cybersource","datatrans":"Datatrans",
    "deutschebank":"Deutschebank","dlocal":"Dlocal","elavon":"Elavon","facilitapay":"Facilitapay",
    "finix":"Finix","fiserv":"Fiserv","fiservemea":"Fiservemea","fiuu":"Fiuu","forte":"Forte",
    "getnet":"Getnet","gigadat":"Gigadat","globalpay":"Globalpay","hipay":"Hipay",
    "iatapay":"Iatapay","itaubank":"ItauBank","jpmorgan":"Jpmorgan","loonio":"Loonio",
    "mollie":"Mollie","moneris":"Moneris","multisafepay":"Multisafepay","nexinets":"Nexinets",
    "nexixpay":"Nexixpay","nmi":"Nmi","noon":"Noon","novalnet":"Novalnet","nuvei":"Nuvei",
    "paybox":"Paybox","payload":"Payload","paypal":"Paypal","paysafe":"Paysafe","payu":"Payu",
    "peachpayments":"Peachpayments","powertranz":"PowerTranz","redsys":"Redsys","shift4":"Shift4",
    "silverflow":"Silverflow","square":"Square","stax":"Stax","stripe":"Stripe",
    "tesouro":"Tesouro","trustpay":"Trustpay","trustpayments":"TrustPayments","tsys":"Tsys",
    "volt":"Volt","wellsfargo":"WellsFargo","worldpay":"WorldPay","worldpayvantiv":"Worldpayvantiv",
    "worldpayxml":"Worldpayxml","xendit":"Xendit","zift":"Zift",
}

FLOW_FEATURE_LIST: dict[str, str] = {
    "Overcapture":"INCLUDE.OVERCAPTURE","IncrementalAuth":"INCLUDE.INCREMENTAL_AUTH",
    "ManualRetryPaymentEnabled":"INCLUDE.MANUAL_RETRY","ManualRetryPaymentDisabled":"INCLUDE.MANUAL_RETRY",
    "ManualRetryPaymentCutoffExpired":"INCLUDE.MANUAL_RETRY",
    "MandatesUsingNTIDProxy":"INCLUDE.MANDATES_USING_NTID_PROXY",
    "DDCRaceConditionClientSide":"INCLUDE.DDC_RACE_CONDITION",
    "DDCRaceConditionServerSide":"INCLUDE.DDC_RACE_CONDITION",
    "PaymentWebhook":"INCLUDE.PAYMENTS_WEBHOOK",
}

# Flows where a connector may be explicitly EXCLUDED (should not run even if config exists)
# Maps flow name → the EXCLUDE.* key in Utils.js CONNECTOR_LISTS
FLOW_EXCLUDE_LIST: dict[str, str] = {
    "ConnectorAgnosticNTID": "EXCLUDE.CONNECTOR_AGNOSTIC_NTID",
}

class CoverageStatus:
    COVERED="COVERED"; MISSING="MISSING"; MISSING_CONFIG="MISSING_CONFIG"
    NOT_IN_ALLOWLIST="NOT_IN_ALLOWLIST"; MISSING_TEST="MISSING_TEST"
    NEEDS_LLM_CHECK="NEEDS_LLM_CHECK"; PARTIAL="PARTIAL"

class FlowType:
    CORE_ONLY="CORE_ONLY"; CONNECTOR_ONLY="CONNECTOR_ONLY"
    CORE_THEN_CONNECTOR="CORE_THEN_CONNECTOR"

CONNECTOR_BODY_SIGNALS={"amount_to_capture","capture_method","payment_method_data","bank_account","wallet","mandate_id","connector"}
CONNECTOR_EXPECTED={"succeeded","processing","partially_captured","charged","pending","client_secret"}
CORE_EXPECTED={"genericunauthorized","unauthorized","missing required","invalid","not found","already exists","ir_","he_","400","401","403","404","422"}
CORE_PREREQ_KINDS={"auth_context","state_setup","request_field","idempotency"}
CONNECTOR_PREREQ_KINDS={"connector_config","payment_method","currency_support","concrete_type"}

def norm_connector(name:str)->str: return CONNECTOR_NAME_MAP.get(name.strip().lower(),name.strip())
def norm_path(path:str)->str:
    path=re.sub(r"\{[^}]+\}",":id",path); return re.sub(r":[a-z_]+",":id",path)
def extract_trigger_paths(flow:dict)->list:
    paths=set()
    for cases in flow.get("trigger_payloads",{}).values():
        for case in cases.values():
            ep=case.get("endpoint","");
            if " " in ep: ep=ep.split(" ",1)[1]
            paths.add(norm_path(ep))
    return sorted(paths)
def extract_setup_paths(flow:dict)->list:
    paths=set()
    for sp in flow.get("setup_payloads",[]):
        ep=sp.get("endpoint","")
        if " " in ep: ep=ep.split(" ",1)[1]
        paths.add(norm_path(ep))
    return sorted(paths)
def extract_handlers(flow:dict)->list:
    return [e.get("handler","") for e in flow.get("endpoints",[]) if e.get("handler")]

def classify_flow(flow:dict)->dict:
    core_score=connector_score=0; signals=[]
    def bs(b): return json.dumps(b).lower()
    for sp in flow.get("setup_payloads",[]):
        body,ep=sp.get("body",{}),sp.get("endpoint","")
        if body.get("confirm") is False: core_score+=2; signals.append("setup:confirm=false")
        elif body.get("confirm") is True: connector_score+=3; signals.append("setup:confirm=true")
        if any(s in bs(body) for s in ("card_number","payment_method_data")):
            if body.get("confirm") is False: core_score+=1
            else: connector_score+=2
        if body.get("customer_id"): core_score+=1
    for handler,cases in flow.get("trigger_payloads",{}).items():
        for case_name,case in cases.items():
            body=case.get("body",{}); exp=case.get("expected_result","").lower()
            if any(s in exp for s in CONNECTOR_EXPECTED): connector_score+=2; signals.append("trigger:connector status")
            if any(s in exp for s in CORE_EXPECTED): core_score+=2; signals.append("trigger:core error")
            cf=CONNECTOR_BODY_SIGNALS&set(body.keys())
            if cf: connector_score+=len(cf); signals.append("trigger body:"+str(cf))
            if body.get("customer_id") and not cf: core_score+=1
    for p in flow.get("prerequisites",[]):
        kind=p.get("kind","")
        if kind in CORE_PREREQ_KINDS: core_score+=1
        if kind in CONNECTOR_PREREQ_KINDS: connector_score+=3; signals.append("prereq:"+kind)
    for e in flow.get("endpoints",[]):
        h=e.get("handler","").lower()
        if any(c in h for c in ("capture","confirm","sync","refund","authorize","void")): connector_score+=1
        if any(c in h for c in ("validate","check","verify","access","auth","duplicate")): core_score+=1
    setup_bodies=[sp.get("body",{}) for sp in flow.get("setup_payloads",[])]
    has_core=any(b.get("confirm") is False for b in setup_bodies)
    has_conn=any(b.get("confirm") is True  for b in setup_bodies)
    is_mixed=has_core and has_conn
    core_spec_hint=None
    if connector_score==0: flow_type=FlowType.CORE_ONLY; needs_connector=False
    elif core_score==0 and not is_mixed: flow_type=FlowType.CONNECTOR_ONLY; needs_connector=True
    elif is_mixed or (has_core and connector_score>=2):
        flow_type=FlowType.CORE_THEN_CONNECTOR; needs_connector=True
        if any("/payments" in sp.get("endpoint","") for sp in flow.get("setup_payloads",[])):
            core_spec_hint="00006-NoThreeDSManualCapture"
    elif core_score>connector_score:
        flow_type=FlowType.CORE_ONLY if connector_score<2 else FlowType.CORE_THEN_CONNECTOR
        needs_connector=connector_score>=2
    else: flow_type=FlowType.CONNECTOR_ONLY; needs_connector=True
    return {"flow_type":flow_type,"core_score":core_score,"connector_score":connector_score,
            "needs_connector_config":needs_connector,"core_spec_hint":core_spec_hint,"signals":signals}

@dataclass
class ConnectorCheck:
    connector:str; flow_name:str; status:str
    raw_block:str=""
    allowlist_key:str=""; allowlist_current:list=field(default_factory=list)
    reference_connector:str=""; reference_block:str=""

@dataclass
class CandidateSpec:
    spec_file:str; spec_name:str; test_names:list; endpoints_hit:list

@dataclass
class FlowCoverageResult:
    flow_id:int=0; description:str=""; connector:str=""; flow_name:str=""
    trigger_paths:list=field(default_factory=list)
    setup_paths:list=field(default_factory=list)
    handlers:list=field(default_factory=list)
    flow_type:str=FlowType.CORE_ONLY
    classification:dict=field(default_factory=dict)
    status:str=CoverageStatus.MISSING
    what_to_fix:list=field(default_factory=list)
    connector_check:Optional[ConnectorCheck]=None
    candidates:list=field(default_factory=list)
    spec_to_create:Optional[str]=None
    core_spec_hint:Optional[str]=None
    existing_it_blocks:dict=field(default_factory=dict)
    flow:dict=field(default_factory=dict)

    @property
    def covered(self)->bool: return self.status==CoverageStatus.COVERED

    def summary(self)->str:
        c=self.classification
        id_str=f"flow_id={self.flow_id}" if self.flow_id else f"{self.connector}/{self.flow_name}"
        lines=[
            f"[{self.status}] {id_str}  strategy={self.flow_type}",
            f"  core={c.get('core_score',0)}  connector={c.get('connector_score',0)}",
            f"  trigger paths: {self.trigger_paths}",
        ]
        for fix in self.what_to_fix: lines.append(f"  -> {fix}")
        if self.connector_check:
            cc=self.connector_check
            lines.append(f"  connector check: {cc.status} for {cc.connector}/{cc.flow_name}")
        if self.candidates:
            for cand in self.candidates[:2]:
                lines.append(f"  candidate: {cand.spec_file} ({len(cand.test_names)} tests)")
        if self.existing_it_blocks:
            total=sum(len(v["it_blocks"]) for v in self.existing_it_blocks.values())
            lines.append(f"  existing it() blocks: {total} across {len(self.existing_it_blocks)} spec(s)")
        return "\n".join(lines)



# ── real JSON format normalisation ───────────────────────────────────────────

def extract_connector_from_flow(flow: dict) -> str:
    """
    Extract connector name from the real JSON format.
    Sources (in priority order):
      1. flow.specialization.connector
      2. flow.prerequisites where kind="concrete_type"
      3. trigger_payload.body.connector
    """
    spec = flow.get('specialization', {})
    if spec.get('connector'):
        return spec['connector']
    for p in flow.get('prerequisites', []):
        if p.get('kind') == 'concrete_type' and p.get('required_value'):
            return p['required_value']
    body = flow.get('trigger_payload', {}).get('body', {})
    c = body.get('connector', '')
    return c.capitalize() if c else ''


def normalise_flow(flow: dict, top_level: dict = None) -> dict:
    """
    Normalise one flow from data['flows'] into the shape flow_query expects.

    Real format has:
      trigger_payload   (singular, one endpoint + body)
      connectors        (list of all affected connectors, may be 100+)
      specialization    (which connector+trait this is for)
      chain             (call chain from handler to changed function)

    Normalised output matches what classify_flow() and check_coverage() expect.
    """
    if top_level is None:
        top_level = {}

    connector = extract_connector_from_flow(flow)

    # trigger_payload → trigger_payloads map
    tp      = flow.get('trigger_payload', {})
    ep_raw  = tp.get('endpoint', '')
    ep_body = tp.get('body', {})
    handler = (flow['endpoints'][0]['handler']
               if flow.get('endpoints') else 'handler')

    trigger_payloads = {
        handler: {
            'success_case': {
                'endpoint':        ep_raw,
                'body':            ep_body,
                'expected_result': 'connector processes request successfully',
            }
        }
    }

    # connectors list — who is affected by the changed function
    # connector_count == 0 means only the specialization connector
    # connector_count > 0 means all listed connectors share the code path
    connectors_affected = flow.get('connectors', [])
    if not connectors_affected and connector:
        connectors_affected = [connector.lower()]

    return {
        # Identity
        'flow_id':             flow.get('flow_id', 0),
        'description':         flow.get('description', ''),
        'changed_function':    flow.get('changed_function',
                                        top_level.get('changed_function', '')),
        'changed_file':        flow.get('changed_file',
                                        top_level.get('changed_file', '')),
        'changed_line':        flow.get('changed_line',
                                        top_level.get('changed_line', 0)),

        # Connector — extracted, NOT from user input
        'connector':           connector,
        'connectors_affected': connectors_affected,
        'connector_count':     flow.get('connector_count', 0),

        # Pipeline-compatible fields
        'endpoints':           flow.get('endpoints', []),
        'prerequisites':       flow.get('prerequisites', []),
        'setup_payloads':      flow.get('setup_payloads', []),
        'trigger_payloads':    trigger_payloads,

        # Guard/condition metadata
        'has_guards':          flow.get('conditions_high', 0) > 0,
        'conditions_high':     flow.get('conditions_high', 0),
        'conditions_missing':  flow.get('conditions_missing', 0),

        # Full chain for LLM context
        'chain':               flow.get('chain', []),
        'specialization':      flow.get('specialization', {}),
    }


def normalise_document(data: dict) -> list[dict]:
    """
    Takes the full top-level JSON document and returns a list of
    normalised flows ready for check_coverage().

    The document has:
      data['flows']   — list of individual flows (one per endpoint)
      data['changed_function'] — what Rust function changed
    """
    flows = data.get('flows', [])
    return [normalise_flow(f, data) for f in flows]


class FlowQueryEngine:
    def __init__(self,uri="bolt://localhost:7687",auth=("neo4j","Hyperswitch123")):
        self.driver=GraphDatabase.driver(uri,auth=auth)
    def close(self): self.driver.close()

    def check_connector_flow(self,connector:str,flow_name:str,endpoint:str="")->FlowCoverageResult:
        """Shape A: check connector+flow. Replaces query.py test_exists()."""
        c=norm_connector(connector)
        ep=self._norm_ep(endpoint)
        fgl=FLOW_FEATURE_LIST.get(flow_name)
        flow={"description":flow_name,"endpoints":[{"handler":flow_name.lower()}],
              "setup_payloads":[],"trigger_payloads":{flow_name.lower():{}},"prerequisites":[]}
        cl=classify_flow(flow)
        result=FlowCoverageResult(connector=c,flow_name=flow_name,
            flow_type=FlowType.CONNECTOR_ONLY,classification=cl,flow=flow)
        with self.driver.session() as s:
            r_a=s.run("""
                MATCH (c:Connector {name:$c})-[:HAS_CONFIG]->(fc:FlowConfig)-[:FOR_FLOW]->(f:Flow {name:$f})
                RETURN fc.raw_block AS rb
            """,c=c,f=flow_name).single()
            if not r_a:
                result.status=CoverageStatus.MISSING_CONFIG
                result.what_to_fix=["Add `"+flow_name+"` block to `configs/Payment/"+c+".js`"]
                ref=s.run("""
                    MATCH (rc:Connector)-[:HAS_CONFIG]->(fc:FlowConfig)-[:FOR_FLOW]->(f:Flow {name:$f})
                    WHERE rc.name<>$c
                      AND NOT fc.raw_block STARTS WITH '{'
                      AND NOT fc.raw_block CONTAINS '// commenting out'
                    RETURN rc.name AS name, fc.raw_block AS block
                    ORDER BY rc.name
                    LIMIT 1
                """,f=flow_name,c=c).single()
                result.connector_check=ConnectorCheck(connector=c,flow_name=flow_name,
                    status=CoverageStatus.MISSING_CONFIG,
                    reference_connector=ref["name"] if ref else "",
                    reference_block=ref["block"] if ref else "")
                return result
            raw=r_a["rb"] or ""

            # Fix 3: check EXCLUDE list — if connector is explicitly excluded, stop
            fexcl=FLOW_EXCLUDE_LIST.get(flow_name)
            if fexcl:
                r_excl=s.run(
                    "MATCH (c:Connector {name:$c})-[:IN_LIST]->(l:FeatureList {key:$k}) RETURN l",
                    c=c,k=fexcl).single()
                if r_excl:
                    result.status=CoverageStatus.COVERED  # excluded = intentionally skip
                    result.what_to_fix=[connector+" is in EXCLUDE list for "+flow_name]
                    result.connector_check=ConnectorCheck(
                        connector=c,flow_name=flow_name,
                        status=CoverageStatus.COVERED,raw_block=raw)
                    return result

            if fgl:
                r_b=s.run("MATCH (c:Connector {name:$c})-[:IN_LIST]->(l:FeatureList {key:$k}) RETURN l.connectors AS m",
                    c=c,k=fgl).single()
                if not r_b:
                    cur=s.run("MATCH (l:FeatureList {key:$k}) RETURN l.connectors AS m",k=fgl).single()
                    result.status=CoverageStatus.NOT_IN_ALLOWLIST
                    result.what_to_fix=['Add "'+c.lower()+'" to CONNECTOR_LISTS.'+fgl+' in Utils.js']
                    result.connector_check=ConnectorCheck(connector=c,flow_name=flow_name,
                        status=CoverageStatus.NOT_IN_ALLOWLIST,raw_block=raw,
                        allowlist_key=fgl,allowlist_current=(cur["m"] if cur else []))
                    return result
            r_c=s.run("""
                MATCH (t:TestCase)-[:TESTS_FLOW]->(f:Flow {name:$f})
                RETURN t.test_name AS name,t.spec_file AS sf,t.line AS line,t.skipped AS sk
                ORDER BY t.spec_file,t.line
            """,f=flow_name).data()
            if not r_c:
                result.status=CoverageStatus.MISSING_TEST
                result.what_to_fix=["Add it() block for "+flow_name+" to relevant spec"]
                result.connector_check=ConnectorCheck(connector=c,flow_name=flow_name,
                    status=CoverageStatus.MISSING_TEST,raw_block=raw)
                return result
            result.status=CoverageStatus.COVERED
            result.connector_check=ConnectorCheck(connector=c,flow_name=flow_name,
                status=CoverageStatus.COVERED,raw_block=raw)
            result.existing_it_blocks={r_c[0]["sf"]:{"endpoints_covered":[],
                "it_blocks":[{"name":r["name"],"line":r["line"],"skipped":r["sk"]} for r in r_c]}}
            return result

    def check_coverage(self,flow:dict,connector:str="")->FlowCoverageResult:
        """Shape B: check flow_id JSON coverage."""
        flow_id=flow.get("flow_id",0); description=flow.get("description","")
        trigger_paths=extract_trigger_paths(flow); setup_paths=extract_setup_paths(flow)
        handlers=extract_handlers(flow); cl=classify_flow(flow)
        flow_type=cl["flow_type"]; c=norm_connector(connector) if connector else ""
        result=FlowCoverageResult(flow_id=flow_id,description=description,connector=c,
            trigger_paths=trigger_paths,setup_paths=setup_paths,handlers=handlers,
            flow_type=flow_type,classification=cl,core_spec_hint=cl.get("core_spec_hint"),flow=flow)
        result.existing_it_blocks=self.get_it_blocks_for_endpoints(trigger_paths)
        result.classification["existing_it_blocks"]=result.existing_it_blocks
        if c and flow_type in (FlowType.CONNECTOR_ONLY,FlowType.CORE_THEN_CONNECTOR):
            fn=self._derive_flow_name(trigger_paths,c)
            if fn:
                cc=self.check_connector_flow(c,fn)
                result.connector_check=cc.connector_check
                if not cc.covered:
                    result.status=cc.status; result.what_to_fix=cc.what_to_fix; return result
        candidates=self._find_covering_specs(trigger_paths)
        result.candidates=candidates
        if not candidates:
            result.status=CoverageStatus.MISSING
            result.what_to_fix=["No spec covers trigger endpoints: "+str(trigger_paths),
                                 "Create new spec for flow_id="+str(flow_id)]
            result.spec_to_create=self._suggest_spec_name(flow)
            return result
        result.status=CoverageStatus.NEEDS_LLM_CHECK
        result.what_to_fix=[str(len(candidates))+" candidate(s) — LLM must verify scenario"]
        return result

    def get_it_blocks_for_endpoints(self,paths:list)->dict:
        if not paths: return {}
        with self.driver.session() as s:
            rows=s.run("""
                MATCH (t:TestCase)-[:CALLS_ENDPOINT]->(e:Endpoint)
                WHERE ANY(p IN $paths WHERE e.path=p
                    OR e.path CONTAINS split(p,'/:id')[0]
                    OR p CONTAINS split(e.path,'/:id')[0])
                RETURN t.spec_file AS sf,t.test_name AS name,t.line AS line,
                       t.skipped AS skipped,e.path AS ep
                ORDER BY t.spec_file,t.line
            """,paths=paths).data()
        res:dict={}
        for row in rows:
            sf=row["sf"]
            if sf not in res: res[sf]={"endpoints_covered":set(),"it_blocks":[],"_seen":set()}
            res[sf]["endpoints_covered"].add(row["ep"])
            if row["name"] not in res[sf]["_seen"]:
                res[sf]["it_blocks"].append({"name":row["name"],"line":row["line"],"skipped":row["skipped"]})
                res[sf]["_seen"].add(row["name"])
        for sf in res: res[sf]["endpoints_covered"]=sorted(res[sf]["endpoints_covered"]); del res[sf]["_seen"]
        return res


    def get_reference_blocks(self, flow_name: str, exclude_connector: str = "",
                              limit: int = 5) -> list[dict]:
        """
        Find existing config blocks for a flow across all connectors.
        Returns blocks ordered by size ASC (simplest/shortest first = best reference).
        Filters out commented-out blocks so LLM gets real usable examples.

        Used to give LLM the most common pattern for a flow it needs to generate,
        rather than relying on a single alphabetically-first connector.
        """
        with self.driver.session() as s:
            rows = s.run("""
                MATCH (c:Connector)-[:HAS_CONFIG]->(fc:FlowConfig)
                      -[:FOR_FLOW]->(f:Flow {name:$flow})
                WHERE ($exclude = "" OR c.name <> $exclude)
                  AND NOT fc.raw_block CONTAINS '// commenting out'
                  AND NOT fc.raw_block STARTS WITH '{'
                  AND size(fc.raw_block) > 50
                RETURN fc.raw_block AS block, c.name AS connector
                ORDER BY size(fc.raw_block) ASC
                LIMIT $limit
            """, flow=flow_name, exclude=exclude_connector or "", limit=limit).data()
        return [{"connector": r["connector"], "block": r["block"]} for r in rows]

    def coverage_gap(self,flow_name:str)->dict:
        with self.driver.session() as s:
            hc=[r["name"] for r in s.run("MATCH (c:Connector)-[:SUPPORTS_FLOW]->(f:Flow {name:$f}) RETURN c.name AS name ORDER BY c.name",f=flow_name).data()]
            ht=[r["name"] for r in s.run("MATCH (t:TestCase)-[:TESTS_FLOW]->(f:Flow {name:$f}) MATCH (c:Connector)-[:SUPPORTS_FLOW]->(f) RETURN DISTINCT c.name AS name ORDER BY c.name",f=flow_name).data()]
        return {"flow":flow_name,"has_config":hc,"has_test":ht,"missing_test":[c for c in hc if c not in ht]}

    def connector_profile(self,connector:str)->dict:
        c=norm_connector(connector)
        with self.driver.session() as s:
            af=[r["flow"] for r in s.run("MATCH (c:Connector {name:$c})-[:SUPPORTS_FLOW]->(f:Flow) RETURN f.name AS flow ORDER BY f.name",c=c).data()]
            te=[r["flow"] for r in s.run("MATCH (c:Connector {name:$c})-[:SUPPORTS_FLOW]->(f:Flow) MATCH (t:TestCase)-[:TESTS_FLOW]->(f) RETURN DISTINCT f.name AS flow ORDER BY f.name",c=c).data()]
            il=[r["key"] for r in s.run("MATCH (c:Connector {name:$c})-[:IN_LIST]->(l:FeatureList) RETURN l.key AS key ORDER BY l.key",c=c).data()]
        return {"connector":c,"total_flows":len(af),"flows_tested":te,"flows_untested":[f for f in af if f not in te],"in_lists":il}

    def get_similar_spec(self,trigger_paths:list,repo_root:str)->Optional[str]:
        with self.driver.session() as s:
            rows=s.run("MATCH (t:TestCase)-[:CALLS_ENDPOINT]->(e:Endpoint) WHERE ANY(p IN $paths WHERE e.path CONTAINS '/payments') RETURN DISTINCT t.spec_file AS sf ORDER BY sf LIMIT 1",paths=trigger_paths).data()
        if not rows: return None
        p=Path(repo_root)/rows[0]["sf"]
        return p.read_text(encoding="utf-8",errors="replace") if p.exists() else None

    def _norm_ep(self,ep:str)->str:
        ep=ep.strip(); m=re.match(r"^(GET|POST|PUT|PATCH|DELETE):(.+)$",ep,re.I)
        return m.group(2).strip() if m else ep

    def _derive_flow_name(self,trigger_paths:list,connector:str)->Optional[str]:
        """
        Derive flow name from trigger path.

        Strategy 1 (connector has existing config):
          Ask Neo4j which Flow the connector's FlowConfig maps to for this endpoint.
          Works when the connector already has the flow configured.

        Strategy 2 (connector missing config — the interesting case):
          Fall back to a static reverse lookup: endpoint path → canonical flow name.
          This is what makes MISSING_CONFIG detectable — if we only used Neo4j,
          a connector with no IncrementalAuth config would return None and skip codegen.
        """
        from indexer import FLOW_ENDPOINTS
        # Build reverse map: normalised path → flow name
        path_to_flow: dict[str, str] = {}
        for fn, eps in FLOW_ENDPOINTS.items():
            for ep in eps:
                p = self._norm_ep(ep["path"])
                if p not in path_to_flow:
                    path_to_flow[p] = fn

        # Strategy 1: ask Neo4j (works if connector already has this flow)
        with self.driver.session() as s:
            for tp in trigger_paths:
                rows=s.run("""
                    MATCH (c:Connector {name:$c})-[:HAS_CONFIG]->(fc:FlowConfig)-[:FOR_FLOW]->(f:Flow)
                    MATCH (fc)-[:CALLS_ENDPOINT]->(e:Endpoint)
                    WHERE e.path=$p OR e.path CONTAINS split($p,'/:id')[0]
                    RETURN f.name AS name LIMIT 1
                """,c=connector,p=tp).data()
                if rows: return rows[0]["name"]

        # Strategy 2: static reverse lookup (works even if connector has NO config yet)
        for tp in trigger_paths:
            if tp in path_to_flow:
                return path_to_flow[tp]
            # Partial match for paths with params
            for known_path, fn in path_to_flow.items():
                base = known_path.split("/:id")[0]
                if base and base in tp:
                    return fn

        return None

    def _find_covering_specs(self,trigger_paths:list)->list:
        if not trigger_paths: return []
        with self.driver.session() as s:
            rows=s.run("""
                UNWIND $paths AS path
                MATCH (t:TestCase)-[:CALLS_ENDPOINT]->(e:Endpoint)
                WHERE e.path=path OR e.path CONTAINS split(path,'/:id')[0] OR path CONTAINS split(e.path,'/:id')[0]
                RETURN path,t.spec_file AS sf,t.spec_name AS sn,
                       collect(DISTINCT {name:t.test_name,line:t.line,skipped:t.skipped}) AS tests
                ORDER BY sf
            """,paths=trigger_paths).data()
        spec_paths=defaultdict(set); spec_tests=defaultdict(list); spec_names={}; seen=defaultdict(set)
        for row in rows:
            sf=row["sf"]; spec_paths[sf].add(row["path"]); spec_names[sf]=row["sn"]
            for t in row["tests"]:
                if t["name"] not in seen[sf]: spec_tests[sf].append(t); seen[sf].add(t["name"])
        required=set(trigger_paths); candidates=[]
        for sf,covered in spec_paths.items():
            if required<=covered:
                tests=sorted(spec_tests[sf],key=lambda x:x.get("line",0))
                candidates.append(CandidateSpec(spec_file=sf,spec_name=spec_names[sf],test_names=[t["name"] for t in tests],endpoints_hit=sorted(covered)))
        if not candidates:
            for sf,covered in spec_paths.items():
                if covered&required:
                    tests=sorted(spec_tests[sf],key=lambda x:x.get("line",0))
                    candidates.append(CandidateSpec(spec_file=sf,spec_name=spec_names[sf],test_names=[t["name"] for t in tests],endpoints_hit=sorted(covered&required)))
        return candidates

    def _suggest_spec_name(self,flow:dict)->str:
        handlers=extract_handlers(flow); flow_id=flow.get("flow_id",0)
        if handlers: name="".join(w.capitalize() for w in "_".join(handlers[:2]).split("_"))
        else: name="".join(w.capitalize() for w in re.findall(r"[A-Za-z]+",flow.get("description",""))[:4])
        num=41
        try:
            with self.driver.session() as s:
                row=s.run("MATCH (sp:SpecFile) WHERE sp.path CONTAINS '/spec/Payment/' RETURN sp.name AS name ORDER BY sp.name DESC LIMIT 1").single()
            if row:
                m=re.match(r"(\d+)",row["name"])
                if m: num=int(m.group(1))+1
        except: pass
        return "cypress/e2e/spec/Payment/"+str(num).zfill(5)+"-"+name+".cy.js"


if __name__=="__main__":
    import argparse,sys
    ap=argparse.ArgumentParser(description="Unified flow query layer")
    sub=ap.add_subparsers(dest="cmd",required=True)
    p1=sub.add_parser("connector"); p1.add_argument("connector"); p1.add_argument("flow"); p1.add_argument("--endpoint",default="")
    p2=sub.add_parser("flow"); p2.add_argument("--file",required=True); p2.add_argument("--connector",default="")
    p3=sub.add_parser("gap"); p3.add_argument("flow")
    p4=sub.add_parser("profile"); p4.add_argument("connector")
    for p in [p1,p2,p3,p4]:
        p.add_argument("--uri",default="bolt://localhost:7687")
        p.add_argument("--user",default="neo4j")
        p.add_argument("--password",default="Hyperswitch123")
    args=ap.parse_args()
    q=FlowQueryEngine(args.uri,(args.user,args.password))
    try:
        if args.cmd=="connector":
            r=q.check_connector_flow(args.connector,args.flow,args.endpoint); print(r.summary())
        elif args.cmd=="flow":
            flow=json.load(sys.stdin if args.file=="-" else open(args.file))
            r=q.check_coverage(flow,connector=args.connector); print(r.summary())
        elif args.cmd=="gap":
            g=q.coverage_gap(args.flow); print("missing_test:",g["missing_test"])
        elif args.cmd=="profile":
            p=q.connector_profile(args.connector); print(p["connector"],"-",p["total_flows"],"flows"); print("untested:",p["flows_untested"][:10])
    finally: q.close()