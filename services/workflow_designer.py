"""
AI Workflow Builder (prototype).

An employee describes a workflow in plain English. Claude returns a structured
proposal (nodes + connections). Python then does everything that has to be
reliable:

  * validates the graph: one trigger, known node types, unique safe IDs,
    connections between real nodes, labelled condition branches, everything
    reachable from the trigger; loops are kept as "returns to" notes
  * applies OpsPilot's human-judgment policy: consequential steps and
    AI-written customer messages get a Human approval step in front of them
  * lays the graph out and routes the connection lines deterministically,
    so the browser only draws validated coordinates and text
  * owns the status lifecycle. AI can only create a proposal. A person saves
    it as a Draft, requests approval, approves it (Active) or disables it.

Nothing here executes workflows. It is a design and approval prototype.
"""
import json
import re
from collections import defaultdict, deque
from typing import List, Literal

from pydantic import BaseModel

from database import now_iso
from services import ai_service
from services.automation_finder import CONSEQUENTIAL, INTAKE_VERBS

MIN_DESCRIPTION, MAX_DESCRIPTION, MAX_NAME = 30, 3000, 80
MAX_NODES = 20

NODE_TYPES = {
    "trigger": {"label": "Trigger", "icon": "play", "css": "wf-trigger"},
    "ai": {"label": "AI", "icon": "sparkle", "css": "wf-ai"},
    "condition": {"label": "Condition", "icon": "split", "css": "wf-condition"},
    "automation": {"label": "Automation", "icon": "zap", "css": "wf-automation"},
    "human_approval": {"label": "Human approval", "icon": "shield", "css": "wf-human"},
    "action": {"label": "Action", "icon": "check", "css": "wf-action"},
}
AI_LEVELS = {"none": "Not involved", "assists": "Assists a person", "performs": "Performs this step"}

STATUSES = ["Draft", "Pending Approval", "Active", "Disabled"]
# action -> (allowed from, becomes, event wording)
TRANSITIONS = {
    "save_draft": ({None, "Draft"}, "Draft", "Saved as draft"),
    "request_approval": ({"Draft", "Disabled"}, "Pending Approval", "Approval requested"),
    "approve": ({"Pending Approval"}, "Active", "Approved and activated"),
    "return_to_draft": ({"Pending Approval"}, "Draft", "Returned to draft"),
    "disable": ({"Active"}, "Disabled", "Disabled"),
}

# Layout geometry (px). The browser uses the same numbers.
NODE_W, NODE_H, H_GAP, V_GAP, PAD = 232, 78, 44, 54, 32
LOOP_GAP = 30

NEGATIVE = re.compile(r"^\s*(no|false|missing|incomplete|fail\w*|reject\w*|not\b|denied|invalid|otherwise)", re.I)
POSITIVE = re.compile(r"^\s*(yes|true|complete|pass\w*|approved|valid|ok)\b", re.I)
WRITES = re.compile(r"\b(draft|drafts|drafting|write|writes|writing|compose|composes|prepare[sd]? a (reply|message|email|response))\b", re.I)
SENDS = re.compile(r"\b(send|sends|sending|deliver|delivers|notify|notifies)\b"
                   r"|(?<!a )(?<!an )(?<!the )\b(email|emails|text|texts|message|messages|reply|replies)\s+(to\s+)?(the|a|each)\s+"
                   r"(customer|client|lead|homeowner)", re.I)
TO_CUSTOMER = re.compile(r"(?<!team )(?<!sales )\b(customer|client|homeowner|lead)s?\b(?!\s+(form|record|list)\b)", re.I)

EXAMPLES = [
    {"key": "documents", "name": "Missing document follow-up", "icon": "file",
     "text": ("When a new project arrives, check whether the required documents exist. If documents are missing, "
              "create a task for Customer Support and request the missing document from the customer. If everything "
              "exists, send the project to a team member for human review.")},
    {"key": "triage", "name": "Customer message triage", "icon": "message",
     "text": ("When a customer emails the support inbox, AI reads the email and works out what they need and how "
              "urgent it is. Urgent messages or unhappy customers go straight to a team lead. For routine questions, "
              "AI drafts a reply and a support agent approves it before it is sent. Every message is logged on the "
              "customer's project.")},
    {"key": "leads", "name": "New lead intake", "icon": "user",
     "text": ("When a new lead form is submitted, check that the contact details are complete. If they are "
              "incomplete, create a task for Sales to call the lead. If they are complete, AI summarizes the "
              "customer's notes. If the roof is older than 15 years, route the lead to Project Review for approval "
              "before a consultation is booked. Otherwise, schedule the consultation automatically and send a "
              "standard confirmation email.")},
]


# --- Structured output schema ---------------------------------------------------------------

class Node(BaseModel):
    id: str
    type: Literal["trigger", "ai", "condition", "automation", "human_approval", "action"]
    title: str
    description: str
    purpose: str
    ai_involvement: Literal["none", "assists", "performs"]
    ai_role: str
    data_used: List[str]
    requires_human_approval: bool
    approver_role: str


class Connection(BaseModel):
    from_id: str
    to_id: str
    label: str


class WorkflowProposal(BaseModel):
    input_assessment: Literal["valid_workflow", "too_vague", "not_a_workflow"]
    clarifying_question: str
    name: str
    summary: str
    nodes: List[Node]
    connections: List[Connection]
    assumptions: List[str]


INSTRUCTIONS = """
You design business workflows for OpsPilot, an internal operations tool for a residential energy services company.
Turn the employee's plain-English description into a workflow proposal. You only PROPOSE; a person reviews it.

input_assessment:
- valid_workflow: describes something that starts, does work and ends (at least 3 steps).
- too_vague: a workflow idea without enough detail. Ask one clarifying_question.
- not_a_workflow: not a business workflow. Say so briefly in clarifying_question.
For anything other than valid_workflow return empty lists and short placeholder text.

For a valid workflow:
- name: 2-5 words. summary: 1-2 plain sentences.
- nodes (3-15). Node types:
    trigger: the single event that starts the workflow. Exactly one.
    ai: AI reads, classifies, summarizes or drafts something. A person can check the result.
    condition: a yes/no or multi-way check. Its title is a short question ending in "?".
    automation: a fixed step software does the same way every time (create a task, update a record, schedule).
    human_approval: a person reviews, decides or approves.
    action: an outcome or hand-off that ends a path (notify, send, hand to a team, close).
- id: short lowercase slug, unique (e.g. "check_docs").
- title: 2-5 words. description: what it does, one sentence. purpose: why it exists, one sentence.
- ai_involvement: none, assists (AI helps a person) or performs (AI does the step). ai_role: one short phrase, or "".
- data_used: 1-5 short names of the information the step reads or writes (e.g. "Project documents").
- requires_human_approval: true if a person must approve before or during this step. approver_role: who, or "".
- connections: from_id -> to_id. Every condition needs one connection per outcome, each with a short label
  ("Yes"/"No", "Urgent"/"Routine"). Other connections use label "". If a path returns to an earlier step,
  include that connection too.

Rules:
- Decisions with real consequences (approvals, money, eligibility, legal or safety calls, exceptions, unhappy
  customers) must go through a human_approval node. AI may prepare information but must not make them.
- Any message AI writes for a customer must be approved by a person before it is sent.
- Only use systems and data the description mentions or OpsPilot clearly has (projects, tasks, documents,
  customers, activity log). Do not invent vendors or tools.
- assumptions: 0-4 short notes about anything you had to assume.
""".strip()


# --- Helpers ------------------------------------------------------------------------------------

def _t(text, limit):
    return ai_service.shorten(re.sub(r"\s+", " ", str(text or "")).strip(), limit)


def _slug(raw, used):
    base = re.sub(r"[^a-z0-9]+", "_", str(raw or "").lower()).strip("_")[:32] or "step"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}_{n}", n + 1
    used.add(slug)
    return slug


HANDOFF = re.compile(r"\b(send|route|assign|escalat|hand|pass|forward|notify|refer)\w*\b[^.]{0,40}\b(team lead|manager|supervisor|"
                     r"team member|agent|reviewer|person|staff|sales|project review|customer support|operations|coordinator)\b", re.I)
# The shared policy's last rule is about *handling* exceptions and escalations. In a workflow, sending that work to a
# person already satisfies it, and a condition that only sorts work ("Urgent?") doesn't decide anything consequential.
DECISION_RULES = [r for r in CONSEQUENTIAL if not r[1].startswith("It handles an exception")]
DECISION_RULES_FOR_CONDITIONS = [r for r in DECISION_RULES if not r[1].startswith("It is a decision that needs judgment")]


def _consequential_reason(node):
    title, description = node["title"], node["description"]
    text = f"{title}. {description}"
    if INTAKE_VERBS.match(title.strip()) or HANDOFF.search(text):
        return None
    rules = DECISION_RULES_FOR_CONDITIONS if node["type"] == "condition" else DECISION_RULES
    for pattern, reason in rules:
        if re.search(pattern, text, re.I):
            return reason
    return None


def _text(node):
    return f"{node['title']}. {node['description']}"


def _ai_writes_customer_message(node):
    """AI composes something for the customer (it may or may not also send it)."""
    t = _text(node)
    return (node["ai_involvement"] != "none" and not INTAKE_VERBS.match(node["title"].strip())
            and bool(WRITES.search(t)) and bool(TO_CUSTOMER.search(t)))


def _sends_to_customer(node):
    t = _text(node)
    return node["type"] not in ("trigger", "human_approval") and bool(SENDS.search(t)) and bool(TO_CUSTOMER.search(t)) \
        and not INTAKE_VERBS.match(node["title"].strip())


def _ai_customer_message(node):  # AI both writes and sends in a single step
    return _ai_writes_customer_message(node) and _sends_to_customer(node)


def _unapproved_path_to_send(nodes, edges, start):
    """A send-to-customer step reachable from `start` without passing a human approval, or None."""
    by_id = {n["id"]: n for n in nodes}
    out = defaultdict(list)
    for e in edges:
        out[e["from"]].append(e["to"])
    seen, queue = {start}, deque(out[start])
    while queue:
        v = queue.popleft()
        if v in seen:
            continue
        seen.add(v)
        n = by_id[v]
        if n["type"] == "human_approval":
            continue
        if _sends_to_customer(n):
            return n
        queue.extend(out[v])
    return None


def _lower_first(text):
    return text if len(text) > 1 and text[1].isupper() else text[:1].lower() + text[1:]


class Invalid(Exception):
    pass


# --- Validation ---------------------------------------------------------------------------------

def validate(parsed: WorkflowProposal):
    """Return a validated workflow definition (dict). Raises Invalid with a user-safe reason."""
    notes = []  # shown to the user under "OpsPilot checks"

    # Nodes: sanitize, unique IDs, per-type rules.
    used, id_map, nodes = set(), {}, []
    for raw in parsed.nodes[:MAX_NODES]:
        title = _t(raw.title, 60)
        if not title:
            continue
        nid = _slug(raw.id, used)
        id_map.setdefault(raw.id, nid)
        node = {"id": nid, "type": raw.type, "title": title, "description": _t(raw.description, 240),
                "purpose": _t(raw.purpose, 240), "ai_involvement": raw.ai_involvement, "ai_role": _t(raw.ai_role, 100),
                "data_used": [], "requires_human_approval": bool(raw.requires_human_approval),
                "approver_role": _t(raw.approver_role, 60), "policy": [], "added_by_policy": False, "returns_to": []}
        seen = set()
        for d in raw.data_used[:6]:
            d = _t(d, 40)
            if d and d.lower() not in seen:
                seen.add(d.lower())
                node["data_used"].append(d)
        if node["type"] == "ai" and node["ai_involvement"] == "none":
            node["ai_involvement"] = "performs"
        if node["type"] == "human_approval":
            node["requires_human_approval"] = True
            if node["ai_involvement"] == "performs":
                node["ai_involvement"] = "assists"
            node["approver_role"] = node["approver_role"] or "Team member"
        if node["type"] == "trigger":
            node["ai_involvement"], node["requires_human_approval"] = "none", False
        nodes.append(node)
    if len(parsed.nodes) > MAX_NODES:
        notes.append({"kind": "info", "text": f"Only the first {MAX_NODES} steps were kept."})

    triggers = [n for n in nodes if n["type"] == "trigger"]
    if not triggers:
        raise Invalid("The proposal had no trigger, so OpsPilot could not tell how the workflow starts.")
    if len(triggers) > 1:
        for extra in triggers[1:]:
            extra["type"] = "action" if not extra["title"].endswith("?") else "condition"
        notes.append({"kind": "fix", "text": f"Kept “{triggers[0]['title']}” as the only trigger."})
    trigger = triggers[0]["id"]
    by_id = {n["id"]: n for n in nodes}

    # Connections: real endpoints, no self-loops, no duplicates, nothing into the trigger.
    edges, seen, dropped = [], set(), 0
    for c in parsed.connections[:60]:
        a, b = id_map.get(c.from_id), id_map.get(c.to_id)
        if not a or not b or a == b or b == trigger or (a, b) in seen:
            dropped += 1
            continue
        seen.add((a, b))
        edges.append({"from": a, "to": b, "label": _t(c.label, 24)})
    if dropped:
        notes.append({"kind": "fix", "text": f"Removed {dropped} connection{'s' if dropped != 1 else ''} that pointed to missing steps, "
                                             "looped a step onto itself or ran back into the trigger."})

    # Reachability and loops (back edges become "returns to" notes).
    out = defaultdict(list)
    for e in edges:
        out[e["from"]].append(e)
    state, back = {}, set()
    def dfs(u):
        state[u] = 1
        for e in out[u]:
            v = e["to"]
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2
    dfs(trigger)
    unreachable = [n for n in nodes if n["id"] not in state]
    if unreachable:
        notes.append({"kind": "fix", "text": "Removed steps that nothing connects to: "
                                             + ", ".join(f"“{n['title']}”" for n in unreachable) + "."})
    nodes = [n for n in nodes if n["id"] in state]
    by_id = {n["id"]: n for n in nodes}
    loops = [e for e in edges if (e["from"], e["to"]) in back]
    edges = [e for e in edges if (e["from"], e["to"]) not in back and e["from"] in by_id and e["to"] in by_id]
    for e in loops:
        by_id[e["from"]]["returns_to"].append({"id": e["to"], "title": by_id[e["to"]]["title"], "label": e["label"]})

    # Conditions need at least two labelled outcomes.
    for n in nodes:
        if n["type"] != "condition":
            continue
        outs = [e for e in edges if e["from"] == n["id"]] + [e for e in loops if e["from"] == n["id"]]
        if len(outs) < 2:
            n["type"] = "automation" if n["ai_involvement"] == "none" else "ai"
            n["policy"].append("Shown as a regular step because the proposal gave it only one outcome.")
            notes.append({"kind": "fix", "text": f"“{n['title']}” had only one outcome, so it is shown as a regular step."})
            continue
        unlabeled = [e for e in outs if not e["label"]]
        if len(outs) == 2 and len(unlabeled) == 2:
            unlabeled[0]["label"], unlabeled[1]["label"] = "Path 1", "Path 2"
            notes.append({"kind": "warn", "text": f"The outcomes of “{n['title']}” were not labelled. Check which path is which."})
        else:
            for i, e in enumerate(unlabeled, 1):
                e["label"] = f"Other {i}" if len(unlabeled) > 1 else "Otherwise"

    _apply_human_policy(nodes, edges, trigger, notes)
    if len(nodes) < 2 or not edges:
        raise Invalid("The proposal did not describe connected steps, so it was not used.")

    layout = route_loops(compute_layout(nodes, edges, trigger), loops)
    counts = {k: sum(1 for n in nodes if n["type"] == k) for k in NODE_TYPES}
    return {"name": _t(parsed.name, MAX_NAME) or "Untitled workflow", "summary": _t(parsed.summary, 400),
            "nodes": nodes, "edges": edges, "loops": loops, "trigger": trigger, "layout": layout, "notes": notes,
            "assumptions": [_t(a, 200) for a in parsed.assumptions[:4] if _t(a, 200)], "counts": counts,
            "ai_nodes": sum(1 for n in nodes if n["ai_involvement"] != "none"),
            "approval_points": sum(1 for n in nodes if n["type"] == "human_approval")}


def _covered(nodes, edges, trigger, target):
    """True if every path from the trigger to `target` passes through a human approval node."""
    kinds = {n["id"]: n["type"] for n in nodes}
    out = defaultdict(list)
    for e in edges:
        out[e["from"]].append(e["to"])
    seen, queue = {trigger}, deque([trigger])
    while queue:
        u = queue.popleft()
        for v in out[u]:
            if v == target:
                return False
            if v not in seen and kinds.get(v) != "human_approval":
                seen.add(v)
                queue.append(v)
    return True


def _topo(nodes, edges, trigger):
    indeg = {n["id"]: 0 for n in nodes}
    for e in edges:
        indeg[e["to"]] += 1
    order, queue = [], deque([trigger])
    out = defaultdict(list)
    for e in edges:
        out[e["from"]].append(e["to"])
    while queue:
        u = queue.popleft()
        order.append(u)
        for v in out[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    return order


def _apply_human_policy(nodes, edges, trigger, notes):
    """Consequential steps and AI-written customer messages need a person first."""
    used = {n["id"] for n in nodes}
    for nid in _topo(nodes, edges, trigger):
        n = next(x for x in nodes if x["id"] == nid)
        if n["type"] in ("trigger", "human_approval"):
            continue
        reason, direct, after = _consequential_reason(n), False, False
        if not reason and _ai_customer_message(n):
            # AI writes AND sends in one step: a person must approve right before it
            reason, direct = "AI writes a message and sends it to the customer", True
        elif not reason and _ai_writes_customer_message(n) and _unapproved_path_to_send(nodes, edges, nid):
            # AI drafts; a later step sends it. A person must approve between the draft and the send.
            reason, after = "AI writes a message that is later sent to the customer", True
        if not reason:
            continue
        if after:
            _insert_approval_after(nodes, edges, n, reason, used, notes)
            continue
        n["requires_human_approval"] = True
        if n["ai_involvement"] == "performs":
            n["ai_involvement"] = "assists"
            n["policy"].append("AI can prepare this step, but a person decides.")
        kinds = {x["id"]: x["type"] for x in nodes}
        parents = [e["from"] for e in edges if e["to"] == nid]
        if (direct and parents and all(kinds[p] == "human_approval" for p in parents)) or \
                (not direct and _covered(nodes, edges, trigger, nid)):
            continue
        aid = _slug(f"approve_{nid}", used)
        title = f"Decide: {n['title'].rstrip('?')}" if n["type"] == "condition" else (
            f"Approve before sending: {n['title']}" if direct else f"Approve: {n['title']}")
        approval = {"id": aid, "type": "human_approval", "title": _t(title, 60),
                    "description": (f"A person reviews the AI-written message before “{n['title']}” sends it." if direct
                                    else f"A person reviews and approves before “{n['title']}” happens."),
                    "purpose": f"{reason}, so OpsPilot requires a person to approve it first.",
                    "ai_involvement": "none", "ai_role": "", "data_used": list(n["data_used"]),
                    "requires_human_approval": True, "approver_role": n["approver_role"] or "Team lead",
                    "policy": ["Added by OpsPilot's human-judgment policy."], "added_by_policy": True, "returns_to": []}
        nodes.insert(nodes.index(n), approval)
        for e in edges:
            if e["to"] == nid:
                e["to"] = aid
        edges.append({"from": aid, "to": nid, "label": ""})
        n["policy"].append(f"{reason}. OpsPilot added “{approval['title']}” before it.")
        notes.append({"kind": "policy", "text": f"Added a human approval before “{n['title']}”: {_lower_first(reason)}."})


def _insert_approval_after(nodes, edges, n, reason, used, notes):
    aid = _slug(f"approve_{n['id']}", used)
    approval = {"id": aid, "type": "human_approval", "title": _t(f"Approve message: {n['title']}", 60),
                "description": "A person reviews and edits the AI-written message before it is sent.",
                "purpose": f"{reason}, so OpsPilot requires a person to approve it first.",
                "ai_involvement": "none", "ai_role": "", "data_used": list(n["data_used"]),
                "requires_human_approval": True, "approver_role": n["approver_role"] or "Team member",
                "policy": ["Added by OpsPilot's human-judgment policy."], "added_by_policy": True, "returns_to": []}
    nodes.insert(nodes.index(n) + 1, approval)
    for e in edges:
        if e["from"] == n["id"]:
            e["from"] = aid
    edges.append({"from": n["id"], "to": aid, "label": ""})
    n["policy"].append(f"{reason}. OpsPilot added “{approval['title']}” after it.")
    notes.append({"kind": "policy", "text": f"Added a human approval after “{n['title']}”: {_lower_first(reason)}."})


# --- Layout (deterministic; the browser only draws these coordinates) ----------------------------

def _branch_rank(label):
    if NEGATIVE.search(label or ""):
        return 0
    if POSITIVE.search(label or ""):
        return 2
    return 1


def compute_layout(nodes, edges, trigger):
    order = _topo(nodes, edges, trigger)
    out, inc = defaultdict(list), defaultdict(list)
    for e in edges:
        out[e["from"]].append(e)
        inc[e["to"]].append(e)
    layer = {trigger: 0}
    for u in order:
        for e in out[u]:
            layer[e["to"]] = max(layer.get(e["to"], 0), layer[u] + 1)
    layers = defaultdict(list)
    for nid in order:
        layers[layer[nid]].append(nid)
    step = NODE_W + H_GAP
    x = {trigger: 0.0}
    for d in range(1, max(layers) + 1 if layers else 1):
        desired = {}
        for nid in layers[d]:
            wants = []
            for e in inc[nid]:
                siblings = sorted(out[e["from"]], key=lambda s: (_branch_rank(s["label"]), order.index(s["to"])))
                k, i = len(siblings), siblings.index(e)
                wants.append(x[e["from"]] + (i - (k - 1) / 2) * step)
            desired[nid] = sum(wants) / len(wants)
        row = sorted(layers[d], key=lambda n: (desired[n], order.index(n)))
        placed = []
        for nid in row:
            pos = desired[nid] if not placed else max(desired[nid], placed[-1] + step)
            placed.append(pos)
        shift = sum(desired[n] for n in row) / len(row) - sum(placed) / len(placed)
        for nid, pos in zip(row, placed):
            x[nid] = pos + shift
    min_x = min(x.values())
    pos = {nid: {"x": round(x[nid] - min_x + PAD), "y": layer[nid] * (NODE_H + V_GAP) + PAD, "layer": layer[nid]}
           for nid in x}
    width = max(p["x"] for p in pos.values()) + NODE_W + PAD
    height = max(p["y"] for p in pos.values()) + NODE_H + PAD
    routes = [route_edge(e, pos, width) for e in edges]
    return {"positions": pos, "edges": routes, "width": width, "height": height,
            "node_w": NODE_W, "node_h": NODE_H}


def route_loops(layout, loops):
    """Dashed 'returns to' lines on the right-hand side, each in its own lane."""
    pos, out = layout["positions"], []
    right_edge = max(p["x"] for p in pos.values()) + NODE_W
    for i, e in enumerate(loops):
        s, t = pos.get(e["from"]), pos.get(e["to"])
        if not s or not t:
            continue
        lane = right_edge + LOOP_GAP * (i + 1)
        sy, ty = s["y"] + NODE_H / 2 + 6, t["y"] + NODE_H / 2 - 6
        pts = [(s["x"] + NODE_W, sy), (lane, sy), (lane, ty), (t["x"] + NODE_W, ty)]
        out.append({"from": e["from"], "to": e["to"], "label": e["label"],
                    "points": [[round(a, 1), round(b, 1)] for a, b in pts],
                    "label_at": {"x": lane, "y": round((sy + ty) / 2, 1)} if e["label"] else None})
    if out:
        layout["width"] = max(layout["width"], right_edge + LOOP_GAP * (len(out) + 1) + PAD)
    layout["loops"] = out
    return layout


def _blocked(xc, y1, y2, pos, skip):
    for nid, p in pos.items():
        if nid in skip:
            continue
        if p["x"] - 8 <= xc <= p["x"] + NODE_W + 8 and not (p["y"] + NODE_H < y1 or p["y"] > y2):
            return True
    return False


def route_edge(e, pos, width):
    """Orthogonal route: down from the source, across, down into the target. Long edges avoid nodes."""
    s, t = pos[e["from"]], pos[e["to"]]
    sx, sy = s["x"] + NODE_W / 2, s["y"] + NODE_H
    tx, ty = t["x"] + NODE_W / 2, t["y"]
    mid = sy + V_GAP / 2
    skip = {e["from"], e["to"]}
    if abs(sx - tx) < 1 and not _blocked(sx, sy, ty, pos, skip):
        pts = [(sx, sy), (tx, ty)]
    elif not _blocked(tx, mid, ty, pos, skip):
        pts = [(sx, sy), (sx, mid), (tx, mid), (tx, ty)]
    else:
        above = ty - V_GAP / 2
        if not _blocked(sx, sy, above, pos, skip):
            pts = [(sx, sy), (sx, above), (tx, above), (tx, ty)]
        else:  # route around the side
            lane = (min(p["x"] for p in pos.values()) - PAD / 2) if tx <= sx else (width - PAD / 2)
            pts = [(sx, sy), (sx, mid), (lane, mid), (lane, above), (tx, above), (tx, ty)]
    label_at = None
    if e["label"]:  # on the last vertical run into the target, just below the turn
        (lx, ly_top), (_, ly_bottom) = pts[-2], pts[-1]
        label_at = {"x": lx, "y": ly_top + min(18, (ly_bottom - ly_top) / 2)}
    return {"from": e["from"], "to": e["to"], "label": e["label"], "points": [[round(a, 1), round(b, 1)] for a, b in pts],
            "label_at": label_at, "branch": _branch_rank(e["label"]) if e["label"] else None}


# --- Persistence and lifecycle ----------------------------------------------------------------------

def validate_input(description, name=""):
    description = description.strip() if isinstance(description, str) else ""
    name = name.strip()[:MAX_NAME] if isinstance(name, str) else ""
    if len(description) < MIN_DESCRIPTION:
        raise ValueError(f"Describe the workflow in a bit more detail (at least {MIN_DESCRIPTION} characters).")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"Keep the description under {MAX_DESCRIPTION:,} characters.")
    return description, name


def log_event(db, design_id, action, actor, details=None):
    db.execute("INSERT INTO workflow_design_events (design_id, action, actor_type, details, created_at) VALUES (?, ?, ?, ?, ?)",
               (design_id, action, actor, json.dumps(details) if details else None, now_iso()))


def generate(db, description, name="", design_id=None):
    """Ask Claude for a proposal, validate it, store it. Returns (design_id | None, data, result)."""
    description, name = validate_input(description, name)
    existing = None
    if design_id is not None:
        existing = db.execute("SELECT * FROM workflow_designs WHERE id = ?", (design_id,)).fetchone()
        if existing is None:
            raise LookupError("That workflow no longer exists.")
        if existing["status"] not in (None, "Draft"):
            raise PermissionError(f"A workflow that is {existing['status']} can't be regenerated. Return it to draft first.")
    user = f"Workflow name from the employee: {name or '(none)'}\n\nWorkflow description:\n{description}"
    parsed, result = ai_service._structured_request("workflow_builder", INSTRUCTIONS, user, WorkflowProposal)
    if parsed.input_assessment != "valid_workflow":
        return None, {"status": parsed.input_assessment, "message": _t(parsed.clarifying_question, 300) or
                      "Describe what starts the workflow, the checks and steps in between, and how it ends."}, result
    try:
        definition = validate(parsed)
    except Invalid as exc:
        raise ai_service.AIServiceError("invalid_output", "AI returned an unusable workflow", str(exc), 502)
    if name:
        definition["name"] = name
    ts = now_iso()
    if existing:
        db.execute("""UPDATE workflow_designs SET description = ?, definition = ?, name = ?, model = ?, response_id = ?,
                      latency_ms = ?, updated_at = ? WHERE id = ?""",
                   (description, json.dumps(definition), name or existing["name"], result.model, result.response_id,
                    result.latency_ms, ts, design_id))
        definition["name"] = name or existing["name"]
    else:
        cur = db.execute("""INSERT INTO workflow_designs (name, description, status, definition, source, model, response_id,
                            latency_ms, created_at, updated_at) VALUES (?, ?, NULL, ?, 'live', ?, ?, ?, ?, ?)""",
                         (definition["name"], description, json.dumps(definition), result.model, result.response_id,
                          result.latency_ms, ts, ts))
        design_id = cur.lastrowid
        db.execute("DELETE FROM workflow_designs WHERE status IS NULL AND created_at < DATETIME('now', 'localtime', '-2 days')")
    log_event(db, design_id, "Workflow proposed by AI" if not existing else "Workflow regenerated by AI", "AI",
              {"model": result.model, "response_id": result.response_id, "nodes": len(definition["nodes"])})
    checks = [n for n in definition["notes"] if n["kind"] in ("fix", "policy", "warn")]
    if checks:
        log_event(db, design_id, f"OpsPilot validated the proposal ({len(checks)} adjustment{'s' if len(checks) != 1 else ''})",
                  "AUTOMATION", {"notes": [n["text"] for n in checks]})
    else:
        log_event(db, design_id, "OpsPilot validated the proposal (no changes needed)", "AUTOMATION")
    return design_id, definition, result


def transition(db, design_id, action, name=None):
    """Human-initiated status change. AI never calls this."""
    row = db.execute("SELECT * FROM workflow_designs WHERE id = ?", (design_id,)).fetchone()
    if row is None:
        raise LookupError("That workflow no longer exists.")
    if not isinstance(action, str) or action not in TRANSITIONS:
        raise PermissionError("Unknown action.")
    allowed, new_status, wording = TRANSITIONS[action]
    if row["status"] not in allowed:
        raise PermissionError(f"A workflow that is {row['status'] or 'unsaved'} can't be changed that way.")
    new_name = (name.strip()[:MAX_NAME] if isinstance(name, str) else "") or row["name"]
    if action in ("request_approval", "approve") and new_name != row["name"]:
        new_name = row["name"]  # the name is locked once it leaves draft
    ts = now_iso()
    extra = ", approved_at = ?" if action == "approve" else ""
    args = [new_status, new_name, ts] + ([ts] if action == "approve" else []) + [design_id]
    db.execute(f"UPDATE workflow_designs SET status = ?, name = ?, updated_at = ?{extra} WHERE id = ?", args)
    if action == "save_draft" and row["status"] == "Draft":
        wording = "Draft saved" if new_name == row["name"] else f"Renamed to “{new_name}”"
    log_event(db, design_id, wording, "HUMAN", {"from": row["status"], "to": new_status})
    return new_status, new_name


def load(db, design_id):
    row = db.execute("SELECT * FROM workflow_designs WHERE id = ?", (design_id,)).fetchone()
    if row is None:
        return None
    events = db.execute("SELECT * FROM workflow_design_events WHERE design_id = ? ORDER BY id DESC LIMIT 20",
                        (design_id,)).fetchall()
    return {"id": row["id"], "name": row["name"], "description": row["description"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "approved_at": row["approved_at"],
            "model": row["model"], "response_id": row["response_id"], "latency_ms": row["latency_ms"],
            "definition": json.loads(row["definition"]),
            "events": [{"action": e["action"], "actor": e["actor_type"], "at": e["created_at"]} for e in events]}


def list_designs(db):
    rows = db.execute("SELECT * FROM workflow_designs WHERE status IS NOT NULL ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = json.loads(r["definition"])
        out.append({"id": r["id"], "name": r["name"], "status": r["status"], "summary": d.get("summary", ""),
                    "nodes": len(d["nodes"]), "ai_nodes": d.get("ai_nodes", 0), "approvals": d.get("approval_points", 0),
                    "counts": d.get("counts", {}), "created_at": r["created_at"], "updated_at": r["updated_at"]})
    return out
