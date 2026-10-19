---
name: distinctive-frontend
description: Design process and quality rules for building interfaces with a clear visual identity instead of generic templates. Use this skill whenever you create or change any user-facing screen — dashboards, admin pages, landing pages, forms, README screenshots, Jinja/HTML templates, React components or CSS — even if the request only says "make a page", "add a dashboard" or "show the data".
---

# Distinctive front end

Generated interfaces tend to look the same: identical rounded cards, soft grey
shadows, a gradient here and there, the same default fonts, every section fading in.
The goal of this skill is the opposite: an interface that looks designed for its
subject and audience, while staying accessible and easy to use.

## Process (do not skip steps)

1. **Brief.** Write three lines before any code:
   - Subject and audience (e.g. "barbershop owner checking today's bookings on a
     phone between clients").
   - The primary job of the screen (the one thing it must make easy).
   - Three adjectives for the personality (e.g. "precise, calm, editorial").
   Look for visual vocabulary in the subject itself: materials, tools, signage,
   documents and habits of that world.
2. **Design plan.** Produce a compact token system:
   - Color: 4 to 6 named hex values (background, surface, text, muted text, one
     accent, one state color), with contrast checked.
   - Type: one or two typefaces chosen for this subject, with their roles and a type
     scale (sizes, weights, line heights).
   - Spacing and radius scales. Radius encodes hierarchy; not everything gets the
     same radius.
   - Layout: an ASCII wireframe for mobile and for desktop, with alignment rules.
   - One memorable element: the single place where the design takes a risk.
3. **Review the plan against the brief.** For each choice ask: "would I have produced
   this for any other project?" If yes, change it and say what changed and why.
4. **Stop for approval** of the brief and plan when the screen is new.
5. **Build** with the tokens as CSS custom properties. No raw hex values or magic
   spacing inside components.
6. **Critique.** Take a screenshot (desktop and mobile), compare with the plan and
   remove one unnecessary decoration before finishing.

## Defaults to avoid unless the brief asks for them

- Content chopped into identical rounded cards with the same shadow.
- Gradient washes used as decoration.
- The same default sans-serif on every project; pick type for the subject.
- Small ALL-CAPS labels above every heading.
- One word in a headline highlighted in another color or weight.
- Numbered markers (01, 02, 03) on content that is not a sequence.
- Fade-and-slide-in on every section and hover animations on every card.
- Arrows appended to every link and button.
- Big number + small label + gradient as the default "stat" block.

These are not forbidden; they are defaults. Use one only when it is the best answer
for this brief, and say why.

## Typography

- Type carries the personality. Set a clear scale with intentional weights.
- Body line length under about 75 characters.
- Use tabular figures for numbers in tables and dashboards so columns align.

## Data-heavy screens (dashboards, tables, reports)

- Lead with the answer to the primary job, not with decoration.
- Right-align numbers, left-align text, keep units in headers.
- Label charts directly instead of relying on legends; start bar charts at zero.
- Show when the data was last updated.
- Design empty, loading and error states for every data region.

## Copy

- Write from the user's point of view, in plain words and sentence case.
- Buttons say exactly what happens ("Cancel appointment", not "Submit"), and the
  result uses the same verb ("Appointment cancelled").
- Errors explain what happened and how to fix it; they do not apologize or blame.
- Empty states invite the next action.
- Use real or realistic content, never lorem ipsum.

## Quality floor (always, without announcing it)

- Responsive from 360 px wide upward.
- Visible keyboard focus and full keyboard navigation.
- Text contrast at least WCAG AA.
- `prefers-reduced-motion` respected; motion only where it explains a change.
- Light and dark themes when the page lives inside a themed environment.
- Semantic HTML (headings in order, labels on inputs, buttons for actions).
- Untrusted data rendered with `textContent` or autoescaped templates, never
  `innerHTML` (see the secure-backend skill).

## Deliverable

Every screen change in a PR includes the brief, the token summary, and desktop and
mobile screenshots in the PR description. README screenshots are updated when the
screen changes.
