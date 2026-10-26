---
name: humanize
description: "Cut AI tells from prose someone other than the user reads: docs, chat replies to teammates, PR and MR descriptions, commit messages, outbound drafts. Use for '/humanize', 'make this sound human', 'de-AI this', or before sending text to another person. Not for routine chat with the user, skill or agent files, CLAUDE.md or AGENTS.md, memory files, archives or vendored code."
---

# Humanize

Edit text to remove AI patterns and add human voice.

## Process

1. Scan for the patterns below.
2. Rewrite. Preserve meaning, match intended tone.
3. Add soul (see next section).
4. Self-audit: "What makes this obviously AI generated?" Fix remaining tells.
5. After cutting or adding words, re-read the claim around them: a removed clause may have held it
   together, and an added line may contradict another part.

## Adding soul

Removing patterns is half the job. Sterile, voiceless writing is just as obvious.

- **Have opinions.** React to facts instead of neutrally listing pros and cons.
- **Vary rhythm.** Short sentences. Then longer ones that take their time. Mix it up.
- **Acknowledge complexity.** "Impressive but also kind of unsettling" beats "impressive."
- **Use "I" when it fits.** First person isn't unprofessional.
- **Let some mess in.** Perfect structure looks machine-made.
- **Be specific.** Not "this is concerning" but "there's something unsettling about agents churning away at 3am."

## Patterns to detect and fix

### Content

1. **Puffery.** "pivotal moment", "testament to", "evolving landscape", "setting the stage for", "indelible mark", "deeply rooted". Cut puffery, state what happened.
2. **Name-dropping.** Listing media outlets without context. Pick one, say what was said.
3. **Superficial -ing phrases.** "highlighting...", "ensuring...", "reflecting...", "showcasing...", "fostering...". Delete or expand with real sources.
4. **Promotional language.** "nestled", "vibrant", "breathtaking", "groundbreaking", "renowned", "stunning", "must-visit". Use neutral descriptions.
5. **Vague attributions.** "Experts believe", "Industry reports suggest", "Some critics argue". Name the source or delete.
6. **Formulaic challenges.** "Despite challenges... continues to thrive." Replace with specific facts.

### Language

7. **AI vocabulary.** Additionally, crucial, delve, enduring, enhance, fostering, garner, interplay, intricate, landscape (abstract), pivotal, showcase, tapestry (abstract), testament, underscore, vibrant. Replace with plain words.
8. **Fancy ways to say "is".** "serves as", "stands as", "boasts", "features". Just say "is" or "has".
9. **"Not just X, but Y."** State the point directly instead.
10. **Rule of three.** Forcing ideas into groups of three. Use the natural number.
11. **Synonym cycling.** Protagonist, main character, central figure, hero all in one paragraph. Pick one, repeat it.
12. **False ranges.** "from X to Y" where X and Y aren't on a meaningful scale. List topics directly.

### Style

13. **Em dash overuse.** Avoid em dashes entirely. Use periods or commas only (no parentheses, no en dashes, no hyphen-as-dash substitutes). Em dashes are an AI tell, and reaching for parentheses instead just trades one tell for another. If a thought needs separation, end the sentence or use a comma.
14. **Colon overuse.** Colons are fine before a list or example. Not as mid-sentence connectors. "If you're coming from traditional automation: instead of registering event handlers, you describe conditions" adds nothing with the colon. Rewrite to let the point stand on its own without comparison framing. "Describing when the scheduler should fire works best as plain English." Same meaning, no crutch punctuation.
15. **Boldface overuse.** Don't bold every proper noun or acronym.
16. **Inline-header lists.** The tell is a bold label and colon that restates the line: "**Performance:** Performance improved...". Convert those to prose. A bold lead-in that ends in a period, names the item, and is followed by genuinely new detail ("**Schema in TypeScript.** Tables live in one file.") is fine, not a tell.
17. **Title case headings.** Use sentence case.
18. **Decorative emojis.** Remove from headings and bullets.
19. **Curly quotes.** Replace with straight quotes.

### Communication artifacts

20. **Chatbot phrases.** "I hope this helps!", "Let me know if...", "Of course!", "Certainly!", "Found the smoking gun!" Remove.
21. **Cutoff disclaimers.** "While specific details are limited..." Find sources or remove.
22. **Sycophantic tone.** "Great question! You're absolutely right!" Respond directly.

### Filler

23. **Filler phrases.** "In order to" becomes "To". "Due to the fact that" becomes "Because". "It is important to note that" gets deleted.
24. **Excessive hedging.** "could potentially possibly be argued that it might" becomes "may".
25. **Generic conclusions.** "The future looks bright." State specific plans or facts.

### Jargon

26. **Abstract metaphor nouns.** Substrate, wedge, vector, locus, vantage, nexus, primitive (as noun), harness (as metaphor), surface (as in "API surface"), bedrock, scaffolding (as metaphor), modality, paradigm, gold-plating, ratchet (as metaphor), evacuate (for moving code), endgame, north star, flywheel. These read as technical but usually have a plainer concrete word. "Substrate" becomes "base". "Wedge in" becomes "add". "Vector" becomes "way" or "method". "Gold-plating" becomes "more than the job needs". "Ratchet" becomes the mechanism's real name or "a limit that only tightens". "Evacuate" becomes "move out". "Endgame" becomes "the last phase". Pick the concrete word.

### Plain speech

27. **Say what it does, not how it feels.** "the database stays close at hand", "SQL you can read", "types that follow your schema" name a feeling. The fix names the mechanism or a number: "`.toSQL()` returns the exact string sent to the database", "a column rename fails the build". Ask what the sentence tells the reader to do or know, then write that. If you can't restate it as a concrete instruction, fact, or number, cut it. One more check: if the sentence could appear unchanged in another project's docs, it says nothing about this one. Cut it.
28. **Shorten or split dense sentences.** If the reader has to backtrack to parse a sentence, break it in two or drop clauses. One idea per sentence.
29. **Active voice.** Prefer it. Catch "is/are/was/were + past participle" and name the actor: "queries are validated" becomes "the compiler validates queries", "the file is parsed by the loader" becomes "the loader parses the file". Passive is fine only when the actor is unknown or genuinely doesn't matter.
30. **Cut adverbs, or use a stronger verb.** "runs quickly" becomes "is fast" or the number. "significantly improves" becomes the measured delta. An adverb propping up a weak verb means the verb is wrong.
31. **Prefer the plain word.** "utilize" becomes "use", "leverage" becomes "use", "facilitate" becomes "help", "numerous" becomes "many", "in the event that" becomes "if". The fancier synonym is rarely clearer.

## House rules on top of the patterns

These come from the reader, so they win on a conflict with the list above. Your own writing
standard, if you keep one, wins over both.

**Match the register of the channel.** A chat reply is not a doc. Teammate chat is lowercase-leaning,
contractions, short declaratives, one or two bullets at most. A bulleted spec-dump with bold
field-labels reads as AI even after every pattern above is fixed. A chat message that opens a topic
or asks questions uses sentence case and a greeting; quick replies stay lowercase. No stacked
sub-bullets, bold labels, "Net:" or API words. Formats for a team spec, lines to say on a call, and a
talk or deck: `references/formats.md`.

**Match the reader, too.** For design or PM readers, lead with the visible result and a number they
can see, such as how much shows and at which breakpoint. Leave CSS variables, units and hashes for
the code comment or the spec. For designers on a board, name what is on screen in plain words: no
internal taxonomy (gate, judges, provenance, ontology) and no code-speak (API paths, enum ids,
scanner names).

**Never invent a term.** Coined compound nouns are the tell that recurs most: crux, ground truth,
seeds, tail-tagging, publish contract, faceted model, gating, provenance, BLUF. Use standard product
vocabulary only: deliverable, owner, status, blocker, dependency, next step. If a real term is
unavoidable, define it in the same sentence you first use it; if a name is not established, ask
rather than coining one.

**Cut these tells too:** sentences over 25 words, "X, not Y" antithesis, false balance, summarizing
tails and aphorisms, meta self-labels, stats notation (say "ran it four times"), and academic jargon.
Put the short version up top.

**Write a comment as a two-line mark.** In a design tool, a tracker or on a board, write
`TAG: <imperative>. <one line of why>.` The tags are SHIP to approve, ADD for a missing piece, FIX
for something wrong, FLAG for a risk that asks for no action, and Q for an open question.

**Check names and figures at their source.** Before naming a person or their role, or stating a
figure, a client or a past result in team material, check it where your project records it. Anything
you cannot confirm there goes to the user, not into the text.

**Leave out how the text was made.** Team-facing text, commit bodies included, never mentions
Claude, the assistant, a subagent, an autonomous run, `/loop` or a local config path. Keep the
`Co-Authored-By` trailer that Claude Code adds, and check the body, not the trailer. Don't cite
meetings either. No call quotes, no "the meeting decided", no meeting-date stamps. State the
decision as the team's shared understanding. Never mention a personal meeting-capture tool, its
transcripts or notes drawn from them; ask the user about them in chat. Before a write to a tracker
or a design tool, save the text as a `.md` draft and run
`python "${CLAUDE_PLUGIN_ROOT}/tools/doclint.py" <draft.md>` on it: no hook covers those writes.

**Lead with the point.** Each paragraph opens with its claim, and each header states that claim on
its own. Cover the body, and the header should still tell the reader what to do. Say what a thing is
for before how it works. Give each thing one name, and never let one word mean two things. Put the
current state and your recommendation in separate, labelled sentences. Match your confidence to the
evidence, and never credit a person with a view you have not checked.

**Use the standard template for a planning doc.** Before a PRD, spec, brief, one-pager, RFC or
options doc, find the standard template and use its section names verbatim. Put unsettled scope
first, as an open question, and mark a section you cannot back with evidence "proposed, not agreed".
For each decision, write the call, what the named thing is, why as a mechanism, the options
considered, the cost and what is still unproven. Use a scored matrix only when every cell is
measured. If a draft is rejected twice, stop restyling and ask what kind of document the reader
expects.

Generated output is never prose to edit: `dist/`, `node_modules/`, lockfiles.
