\---

name: cos-agent-test

description: Test the CoS Sales Agent against the 662-email MongoDB dataset using CEO-style questions. Use only the cos-sales-agent MCP connector during evaluation and never bypass it with Gmail, Calendar, or other external connectors.

\---



\# CoS Sales Agent — Evaluation Skill



\## Purpose



This skill defines how to test the current CoS Sales Agent as a CEO would use it.



The current system under test is:



CEO question

&#x20;   ↓

CoS / Claude reasoning

&#x20;   ↓

cos-sales-agent MCP

&#x20;   ↓

MongoDB

&#x20;   ↓

662-email source dataset

&#x20;   ↓

Grounded CEO-style answer



The purpose of this skill is to determine whether the current MCP query layer and CoS reasoning can answer natural CEO questions using the data already ingested into MongoDB.



This is an evaluation skill, not the final production CoS behavior specification.



\---



\# 1. Source of Truth



For this evaluation, MongoDB is the source of truth.



The relevant email dataset contains 662 source emails from:



`bd\_active\_follow\_up\_emails.json`



The dataset was ingested into the CoS Sales Agent MongoDB database.



Important:



\- Do not assume that every MongoDB email came from the 662-email file.

\- The MongoDB database also contains previously processed records.

\- Do not claim an email came from the JSON file unless the available data supports that conclusion.

\- Do not invent missing relationships or metadata.

\- If the available MCP tools cannot establish something, say so.



\---



\# 2. Connector Restrictions



During this evaluation, use ONLY:



`cos-sales-agent`



Do NOT use:



\- Gmail

\- Google Calendar

\- other email connectors

\- other calendar connectors

\- unrelated MCP servers

\- external web search

\- external databases



The purpose is to test whether the CoS Sales Agent can answer questions from its own MongoDB-backed MCP layer.



If Gmail or Calendar appears available, do not use it.



If the requested information cannot be obtained through `cos-sales-agent`, report the limitation instead of switching to another connector.



\---



\# 3. Available MCP Tools



The current `cos-sales-agent` MCP exposes these tools:



\## Email / retrieval



\### `list\_processed\_emails`



Use for listing stored email records.



Important:

Some raw-ingested emails do not have `processing\_status`.



Do not assume every stored email is AI-processed.



\---



\### `search\_emails`



Use to search emails using supported search criteria such as:



\- sender

\- subject text

\- label



Prefer this tool when the CEO asks about emails, conversations, senders, or subjects.



\---



\### `get\_thread`



Use when a question requires understanding the complete conversation rather than a single email.



When `search\_emails` identifies a relevant thread, use `get\_thread` when additional conversation context is necessary.



\---



\# 4. People / Entity Tools



\### `list\_people`



Use when the question concerns:



\- people

\- contacts

\- participants

\- people associated with an organization

\- people associated with a thread



Do not assume that a person's name uniquely identifies one person if the data contains ambiguity.



\---



\### `list\_projects`



Use when the question concerns:



\- projects

\- active projects

\- projects associated with an entity

\- project information



\---



\### `list\_commitments`



Use when the question concerns:



\- commitments

\- promises

\- things we agreed to do

\- customer commitments

\- outstanding commitments



\---



\### `list\_follow\_ups`



Use when the question concerns:



\- follow-ups

\- pending follow-ups

\- things that need follow-up

\- actions after a conversation



\---



\### `list\_meetings`



Use when the question concerns:



\- meetings

\- proposed meetings

\- meeting records

\- meeting status

\- meeting-related information already extracted into MongoDB



Important:



A meeting stored in the meeting collection is not automatically proof that a meeting is confirmed on a calendar.



Do not use Google Calendar to verify it during this evaluation.



\---



\### `get\_project\_summary`



Use for questions requiring a consolidated project-level view.



\---



\### `get\_company\_summary`



Use for questions requiring a consolidated company/customer-level view.



\---



\# 5. CEO Question Handling



CEO questions will normally be vague and natural.



Examples:



\- "What do I need to follow up on?"

\- "What's happening with James?"

\- "What do we know about this customer?"

\- "Are there any possible meetings in Miami?"

\- "What are people waiting on from me?"

\- "Give me a briefing on this customer."

\- "What have we promised this customer?"

\- "What projects are we working on?"

\- "Who is involved in this project?"



Do not require the CEO to provide:



\- email addresses

\- MongoDB IDs

\- thread IDs

\- project IDs

\- MCP tool names

\- collection names

\- database queries



The CoS should attempt to resolve these through the available MCP data.



\---



\# 6. Tool Selection Rules



Use the minimum number of tools necessary, but use multiple tools when the question requires cross-entity reasoning.



Examples:



\### Question



"What have we promised this customer?"



Possible flow:



`get\_company\_summary`

→ identify relevant project/customer context

→ `list\_commitments`



\---



\### Question



"What do I need to follow up on?"



Possible flow:



`list\_follow\_ups`



If additional context is required:



`list\_follow\_ups`

→ `get\_thread`



\---



\### Question



"What's happening with James?"



Possible flow:



`list\_people`

→ identify James

→ `search\_emails`

→ `get\_thread` where needed

→ summarize



Do not ask the CEO for James's email address unless the available data genuinely cannot resolve the person.



\---



\### Question



"Are there any possible meetings in Miami?"



Possible flow:



`search\_emails` for Miami

→ inspect relevant threads

→ `list\_meetings` if relevant

→ distinguish historical, proposed, planned, and confirmed information based only on available data.



Do not use Google Calendar.



\---



\# 7. Cross-Entity Reasoning



The CoS should combine information from multiple MCP tools when required.



Example:



"What do I need to know before talking to this customer?"



The answer may require:



1\. people

2\. company

3\. projects

4\. recent emails

5\. commitments

6\. follow-ups

7\. meetings



Do not answer such questions using only one narrow query if the available MCP tools can provide additional relevant context.



\---



\# 8. Grounding Rules



Every substantive claim should be supported by information retrieved through `cos-sales-agent`.



Do not:



\- invent facts

\- invent dates

\- invent meeting confirmations

\- invent commitments

\- invent people/project relationships

\- assume an email was sent or received if the data does not show it

\- claim something is "upcoming" without supporting date information

\- claim something is "confirmed" without supporting evidence



When evidence is incomplete, explicitly say:



\- "The available data shows..."

\- "I found..."

\- "I couldn't confirm..."

\- "The current MCP data does not establish..."



\---



\# 9. Distinguish Meeting States



When discussing meetings, distinguish:



\- historical meeting

\- meeting discussed

\- meeting proposed

\- meeting planned

\- meeting confirmed

\- meeting date unknown



Do not convert a proposed meeting into a confirmed meeting.



Do not convert an email mention into a calendar event.



\---



\# 10. Distinguish Commitment States



When discussing commitments, distinguish between:



\- something someone explicitly committed to

\- something inferred from an email

\- something already completed

\- something still outstanding



If the underlying MCP data does not provide status, do not invent status.



\---



\# 11. "Waiting for My Reply"



Treat this as a reasoning task, not merely an email search.



A potential "waiting for my reply" conversation should be evaluated using available evidence such as:



\- sender

\- recipients

\- thread sequence

\- latest message

\- whether the latest message is directed to the user

\- whether the user subsequently replied

\- thread status/context



Do not claim that an email is waiting for a reply unless the available evidence supports it.



If the current MCP tools cannot reliably determine this, say:



"The current MCP query layer does not yet expose enough information to reliably determine which threads are waiting for your reply."



Do not use Gmail to fill the gap.



\---



\# 12. CEO-Style Answer Format



Answers should be concise and executive-friendly.



Prefer:



\### Summary



One or two sentences.



\### Key points



\- Person/company

\- What happened

\- Important commitment

\- Follow-up

\- Meeting/date if relevant



\### Open items



Only when relevant.



Avoid:



\- MCP terminology

\- MongoDB terminology

\- database collection names

\- raw JSON

\- long technical explanations



The CEO should receive a business answer, not a technical trace.



\---



\# 13. When Information Is Missing



If the current MCP layer cannot answer the question:



Do NOT:



\- use Gmail

\- use Calendar

\- search the web

\- fabricate an answer



Instead say what is missing.



Example:



> "I can find the relevant emails, but the current MCP layer doesn't reliably determine whether the latest message is awaiting your reply."



This is considered a useful evaluation result because it identifies a capability gap.



\---



\# 14. Evaluation Criteria



Each CEO question should be evaluated on:



\### A. Correct tool selection



Did the CoS select an appropriate MCP tool?



\### B. Correct data retrieval



Did the MCP tool return the relevant MongoDB records?



\### C. Correct entity resolution



Did the CoS correctly connect:



person → email → thread → project → commitment/follow-up/meeting?



\### D. Correct reasoning



Did the CoS interpret the retrieved information correctly?



\### E. Grounding



Is the final answer supported by the retrieved data?



\### F. Completeness



Did the CoS use additional tools when the question required them?



\### G. No connector bypass



Did the CoS avoid Gmail, Calendar, and other external sources?



\### H. CEO usability



Is the answer concise and understandable without technical knowledge?



\---



\# 15. Recommended Evaluation Questions



Use these as the initial test suite.



\## Email



1\. "Show me the latest emails I received."



2\. "What have I been discussing recently with James?"



3\. "Find my recent emails about Miami."



4\. "What did James say in our last conversation?"



\## People / Customers



5\. "Who are the people we're dealing with at The 614 Group?"



6\. "What do we know about The 614 Group?"



7\. "Who is involved with this customer?"



\## Projects



8\. "What projects are we working on with this customer?"



9\. "Give me a quick update on this project."



10\. "Who is involved in this project?"



\## Commitments



11\. "What have we promised this customer?"



12\. "What commitments are still outstanding?"



\## Follow-ups



13\. "What do I need to follow up on?"



14\. "Which follow-ups are still pending?"



15\. "Who are we waiting to hear back from?"



\## Meetings



16\. "What meetings have been discussed in my emails?"



17\. "Are there any possible meetings in Miami?"



18\. "Which meetings are confirmed?"



19\. "Which meetings are only proposed?"



\## Executive questions



20\. "What needs my attention right now?"



21\. "What are other people waiting on from me?"



22\. "Which customer conversations need my attention?"



23\. "Give me a quick briefing on my active customers."



24\. "Give me everything I need to know before my next conversation with this customer."



\---



\# 16. Test Procedure



For every question:



1\. Ask the question exactly as a CEO would.

2\. Do not provide MCP tool names.

3\. Do not provide database IDs unless the CEO naturally has them.

4\. Confirm that only `cos-sales-agent` is used.

5\. Observe which MCP tools are called.

6\. Check the returned data.

7\. Check the final answer against the retrieved data.

8\. Record missing capabilities.

9\. Do not modify MongoDB during evaluation.



\---



\# 17. Important Current-System Limitations



The current MCP layer is a retrieval layer, not a complete autonomous CoS.



Known limitations may include:



\- person-name resolution may require additional logic

\- "waiting for my reply" is not yet a dedicated query capability

\- possible meeting discovery may require searching email content rather than only querying extracted meetings

\- project/person/company relationships may not always be fully connected

\- some commitments may not have project IDs

\- meeting records may contain free-text project information

\- company summaries may miss projects when entity names do not exactly match

\- raw-only emails may not have `processing\_status`

\- MongoDB contains more than the 662 source emails



Treat these as test findings rather than reasons to fabricate an answer.



\---



\# 18. No Write Operations



During evaluation:



\- Do not call `process\_email`.

\- Do not modify emails.

\- Do not create calendar events.

\- Do not send emails.

\- Do not modify MongoDB.

\- Do not trigger AI ingestion.

\- Do not reprocess the 662-email dataset.



This evaluation is READ-ONLY.



\---



\# 19. Final Principle



The CEO should be able to ask:



"What do I need to know?"



rather than:



"Call search\_emails with this sender and then call get\_thread with this ID."



The goal of the CoS is to hide the technical retrieval complexity.



The expected architecture is:



CEO

&#x20;↓

Natural-language question

&#x20;↓

CoS reasoning

&#x20;↓

Select MCP tool(s)

&#x20;↓

MongoDB

&#x20;↓

Structured evidence

&#x20;↓

Cross-entity reasoning

&#x20;↓

Concise CEO answer



The MCP layer provides access to the data.



The CoS reasoning layer determines what the CEO is asking and how multiple pieces of data should be combined.



The evaluation harness determines whether the entire process actually works.

