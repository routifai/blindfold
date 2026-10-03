# Context

The language of the long-running assistant: one Super Chat per user, short
side chats, and sub-agents doing delegated work. This is the backend that will
be the main driver for **NOVA**, the assistant's user-facing app.

## Current scope: frontend and wires only

The backend described here is the contract, not today's work. Right now only
the NOVA frontend and the **Wires** to this backend are being built:

- Build the UI surfaces named below (Chat List, Super Chat view, Side Chats,
  Activity Panel, Agent Identity) and connect each one to its Wire.
- Where a backend capability is not implemented yet, the Wire returns a clear
  "not available yet" state. The UI never fakes behavior the backend doesn't
  have.
- Don't change backend behavior, mode rules or the terms below to suit the
  UI; raise a mismatch as a flagged ambiguity instead.

## The switch: `superside-chat`

Everything in this glossary is one capability, turned on per session with the
setting `omnigent.context.mode=superside-chat`. A session is in exactly one
mode:

| Mode | Meaning |
|---|---|
| unset | Plain Omnigent, unchanged |
| `rollover` | Context management for native CLIs (Claude Code, Codex, Pi), as built before |
| `superside-chat` | The Super Chat, its Side Chats and Sub-agents, on the Claude SDK engine |

Wherever this document says Super Chat, Side Chat, Sub-agent, Activity,
Memory Profile or Rollover in the Super Chat sense, it applies only to
`superside-chat` sessions.

## Ground rule: Omnigent owns everything

The Engine only runs the model loop. Every capability (Sub-agents, Rollover,
Memory, tools, Side Chats, Activity, Results) goes through Omnigent's own
layer, never through an engine feature (no SDK built-in sub-agents, no engine
compaction, no engine memory or CLAUDE.md, no engine session resume). This
keeps the design portable to any backend built on Omnigent and lets the
Engine be swapped.

## Language

**superside-chat**:
The mode that turns on this whole capability for a session. Set when the
session is created; Side Chats and Sub-agents launched from it carry it too.
_Avoid_: super chat mode, superchat flag, side-chat mode

**Super Chat**:
The user's single, long-lived conversation with the assistant. One per user;
every topic and project lives inside it.
_Avoid_: main chat, coordinator session, thread

**Side Chat**:
A short-lived aside branched from the Super Chat to explore one thing without
cluttering it. Starts in one of two ways, chosen when it is created: **with
context** (a summary of the Super Chat) when it continues the current
discussion, or **blank** (only the Memory Profile) when it is unrelated.
_Avoid_: sub-chat, fork, workspace

**Archived** (of a Side Chat):
Ended and hidden from the user's list, but kept and readable. A Side Chat is
archived automatically after a period of inactivity (a setting: one month in
production, one hour for testing). Side Chats are archived, never deleted.
_Avoid_: deleted, discarded, closed

**Sub-agent**:
A helper the Super Chat or a Side Chat sends off to do one task in the
background while it keeps talking with the user. Each Sub-agent is its own
saved session: it survives restarts and its work appears as an Activity. There
is one kind of Sub-agent; a Side Chat's helpers are Sub-agents too.
_Avoid_: worker, child session, task session, side-chat helper

**Sub-agent Type**:
A named kind of Sub-agent (for example researcher, analyst, drafter) defined
in the Super Chat's configuration, fixing its model, reasoning effort and how
many can run at once. A chat launches a Sub-agent by Type, never by naming a
model.
_Avoid_: profile, persona, role, model choice

**Originating Chat**:
The Super Chat or Side Chat that launched a Sub-agent. The Sub-agent's Result
goes back there; if that Side Chat has been Archived, the Result goes to the
Super Chat instead.
_Avoid_: parent, owner, requester

**Brief**:
The task the Originating Chat writes when it launches a sub-agent: what to do, the
outcome wanted, and every fact needed. A sub-agent starts from its Brief and
the Memory Profile only, never from a copy of the Super Chat's conversation.
_Avoid_: prompt, instructions, task message

**Memory**:
What the assistant knows about the user across all chats: durable facts,
preferences and decisions, each backed by the user's own words. Saved two
ways: right away when the user states something lasting, and by Upkeep in the
background. A newer fact replaces an outdated one; the history is kept.
_Avoid_: notes, knowledge base, history

**Upkeep**:
The periodic background job that reads recent conversations (Super Chat, Side
Chats, Sub-agents) and adds to Memory what was not saved explicitly, keeping
only facts backed by the user's own words.
_Avoid_: dreaming, sync, indexing

**Memory Profile**:
The short set of standing facts about the user (preferences, role, standing
instructions) given to the Super Chat every turn and to every sub-agent at
start.
_Avoid_: user profile, portrait, MEMORY.md

**Result**:
The sub-agent's final message, delivered to its Originating Chat when the
sub-agent finishes. The only thing a chat receives from a sub-agent without
asking.
_Avoid_: report, handoff, notification

**Activity**:
One unit of work the assistant did: a Super Chat or Side Chat working through
a request, or a Sub-agent doing its task. Has a title, a one-line outcome, a
time, a status (In Progress, Done, Failed, Cancelled) and its Steps. Read on
demand by the user or a chat, never pushed.
_Avoid_: task, job, run, activity log

**Activity Feed**:
The user's list of every Activity, newest first, grouped by day, in a side
panel next to the conversation. Opening an Activity shows its Steps.
_Avoid_: history, timeline, audit log

**Step**:
One action in an Activity, described in one plain-language line ("Searched
docs for 'side chat'", "Launched a sub-agent to check the totals"), with its
full detail available when opened.
_Avoid_: tool call (in user-facing language), event, span

**Rollover**:
Omnigent shortening a long Super Chat, Side Chat or Sub-agent: older turns are replaced
by a summary written by Omnigent, recent turns are kept word for word, and the
full thread stays readable. Omnigent decides when: when the conversation
reaches its size limit, or on the first message after the chat has been idle
for a set time (refresh on return). The engine's own compaction is not used.
_Avoid_: compaction (as a user-facing word), truncation, reset

**NOVA**:
The user-facing app for the long-running assistant. Its frontend shows the
Super Chat, Side Chats and Activity, and talks to this backend only through
Wires.
_Avoid_: the client, the shell, Muse (the product NOVA is modelled on)

**Wire**:
One connection between a NOVA UI surface and the backend: the request it
makes, the data it reads, and the updates it listens for. Each UI surface has
named Wires; a Wire to a capability that isn't built yet reports "not
available yet".
_Avoid_: endpoint (for the whole surface), integration, binding

**Chat List**:
The NOVA sidebar listing the Super Chat (labelled "Main chat"), then the
user's Side Chats under "Side chats", with search. Archived Side Chats are
hidden from it.
_Avoid_: conversation list, threads

**Agent Identity**:
The assistant's name, avatar and Connection Status, shown at the top of the
Activity Panel. The user can rename it and change the avatar.
_Avoid_: persona card, bot profile

**Connection Status**:
Whether NOVA is live-connected to the backend: Connecting or Connected. It
describes the Wire, not whether the assistant is busy.
_Avoid_: online, presence, agent status

**Activity Panel**:
The side panel next to the conversation. Its first tab is the Activity Feed;
the other tabs (shown as shield, clock and fingerprint icons) are reserved
for permissions, scheduled work and Memory, to be confirmed as the backend
exposes them.
_Avoid_: inspector, drawer, right rail

**Invite**:
A control on the conversation header. A Side Chat is always private 1:1, so
Invite never adds people to a chat; what it shares is to be decided.
_Avoid_: share chat, add participant

**Engine**:
What runs a conversation's model loop. The Super Chat, its Side Chats and its
sub-agents run on the Claude SDK engine.
_Avoid_: harness (in user-facing language)

## Relationships

- A **User** has exactly one **Super Chat**.
- A **Super Chat** has zero or more **Side Chats**; each **Side Chat** belongs to one **Super Chat**. Side Chats branch only from the Super Chat, never from another Side Chat.
- A **Side Chat** sends nothing back to its **Super Chat**; the **Super Chat** reads the Side Chat (recent turns or full thread) when it needs to.
- A **Super Chat** or a **Side Chat** launches zero or more **Sub-agents**; each **Sub-agent** has one **Originating Chat** and delivers one **Result** there.
- When a **Sub-agent** the **User** asked for finishes, its **Originating Chat** tells the **User** right away, even if the **User** is not chatting. Work the assistant started on its own is surfaced only when its **Result** is meaningfully new.
- Every piece of multi-step work, by a chat or a **Sub-agent**, is one **Activity** made of **Steps**; all of a **User**'s **Activities** appear in their **Activity Feed**.
- A **Sub-agent** starts from one **Brief** plus the user's **Memory Profile**.
- A **Sub-agent** launched by a chat may launch its own **Sub-agents** (acting as a coordinator); those cannot launch any further. At most two levels below a chat.
- The **User** can see every **Sub-agent** and open its **Activity**, but cannot message or stop it. Only its **Originating Chat** cancels a **Sub-agent**, and only when the **User** asks it to.

## Example dialogue

> **User:** "Can I check this spreadsheet without messing up our main conversation?"
> **Assistant:** "Opening a **Side Chat** for it. When you're done it's **archived**, and I can still read it later if you ask what we found."

## Flagged ambiguities

- "rollover" means the shortening mechanism only (see **Rollover**). It is not the name of the Super Chat model. The mode value `rollover` (native CLIs) and the mode value `superside-chat` (Super Chat on the Claude SDK) both use Rollover, but they are different modes; never use "rollover" to mean the Super Chat capability.
- NOVA labels the Super Chat **"Main chat"** in the Chat List. That label is UI copy only; in design, code and Wires the term is still **Super Chat**.
- The Activity Panel's icon tabs (shield, clock, fingerprint) are named here by their likely purpose (permissions, scheduled work, Memory). Confirm each against the backend before wiring it.
- Earlier work also covered native CLIs (Claude Code, Codex, Pi), where each CLI compacts itself at Omnigent's threshold. For the Super Chat on the Claude SDK engine, Omnigent writes the summary itself.
