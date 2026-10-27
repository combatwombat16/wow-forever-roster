# wow-forever-roster
Flowmasters wow forever roster site.

# No hardcoded secrets
Source code must not contain passwords, API keys, tokens, or connection URLs with credentials.
Read them from the environment, a function parameter, or the config module.

# Comments explain why, not what
A comment states a reason, a constraint, a workaround, or a non-obvious invariant. A comment
that restates what the next line plainly does is a violation.

# Errors are not swallowed
A `catch` block must handle the error, report it, or re-raise it. An empty catch block, or
one whose body is only a comment, is a violation.

# No partial implementations
Implement features fully. A comment that says "for now", "simplified", or "later", or a
stub body, is a violation. If a part genuinely cannot be done, say so in your reply instead
of stubbing it.

# Do not run destructive commands that erase uncommitted work
`git reset --hard`, `git checkout -- .`, `git clean -fd`, and similar commands that discard
untracked or uncommitted changes are forbidden. These destroy work that has no backup. If a
clean tree is needed, create a worktree instead or ask the user.

# The whole app is one static file
paths: index.html
`index.html` carries the markup, the `<style>` block and the single trailing `<script>`;
there is no build step, bundler, `package.json` or test runner in this repo. Do not add ES
module `import`/`export`, TypeScript, or a framework that would need compilation, and do not
split the app into source files unless the user asks for that change explicitly.

# Third-party assets are limited to Tailwind Play CDN and Google Fonts
paths: index.html
The `<head>` loads only `cdn.tailwindcss.com`, the Cinzel/Inter stylesheet and `preconnect`
links. Adding another CDN script, stylesheet or runtime dependency is a violation unless the
user asked for that dependency.

# Inline event handlers depend on global functions
paths: index.html
`index.html` wires behaviour through `onclick=`/`onsubmit=` attributes, so every function
named in one must stay reachable as a global. Wrapping the script in an IIFE or module, or
renaming or deleting such a function without updating every call site, is a violation. Check
both directions with `grep -o 'onclick="[a-zA-Z]*(' index.html` against the `function`
definitions before finishing.

# Every DOM id used by the script exists in the markup
paths: index.html
Each `document.getElementById('x')` must resolve against an `id="x"` written in the same
file, and no `id` may be duplicated. Adding a render function that reads an id nobody
declared, or renaming an id on one side only, is a violation.

# characters.json is an array of fixed character records
paths: characters.json
The file is a top-level JSON array; every element carries exactly the keys `id`, `player`,
`name`, `track`, `role`, `roleSec`, `race`, `class`, `prof1`, `prof2`. Renaming a key,
changing its type, nesting a record, or wrapping the array in an object is a schema break
that also breaks the external tracker that writes this file, so it needs the user's explicit
approval.

# track is exactly flowmasters, family, or alts
paths: index.html, characters.json
The Squad Filter has one button per value plus `all`, and load-time code rewrites the retired
value `together` to `flowmasters`. Inventing another track value, or dropping the
`together` normalisation, is a violation.

# role and roleSec use the closed role vocabulary
paths: index.html, characters.json
`role` is `Tank`, `Healer`, or `DPS`. `roleSec` is one of those or the literal string `None`
meaning "no off-spec". Party slots, the trinity status and auto-form all compare against
these literals, so a new spelling or a `null`/empty off-spec is a violation.

# race must be a key of FOREVER_RACE_CLASSES
paths: index.html, characters.json
Every record's `race` must be one of the ten races declared in `FOREVER_RACE_CLASSES`, and
its `class` must appear in that race's allowed class list. Faction comes from `ALLIANCE_RACES`
via `getFaction()`, so a race absent from that list silently renders as Horde. Adding a race
means updating `FOREVER_RACE_CLASSES` and `ALLIANCE_RACES` together.

# class must be a key of CLASS_COLORS
paths: index.html, characters.json
The nine classes in `CLASS_COLORS` are the only valid classes. A class outside that set still
renders but loses its colour and falls back to grey, which silently degrades the roster, party
slots and any class breakdown.

# prof1 and prof2 use ALL_PROFESSIONS or None
paths: index.html, characters.json
Professions are the nine strings in `ALL_PROFESSIONS` or the literal `None`, and their short
labels come from `PROF_ABBR`. A profession outside `ALL_PROFESSIONS` is dropped by the
profession matrix coverage maths, so extend both lists in the same change.

# character id is a unique non-empty string
paths: index.html, characters.json
Ids are strings and are the only identity the app has: edit, delete, party membership and
off-spec role all match on `id`. Duplicated or numeric-typed ids let one edit mutate several
records, so they are a violation. New records take `Date.now().toString()`.

# characters plus currentFilter is the single source of truth
paths: index.html
All visible panels derive from the `characters` array filtered by `currentFilter`
(`all` means unfiltered). Do not keep a second copy of roster state, do not persist a derived
view, and do not render a panel from the unfiltered array when it is meant to follow the
Squad Filter — recompute the filtered list inside the render function the way
`renderProfessions()` does.

# Panels that follow the Squad Filter are re-rendered from renderRoster
paths: index.html
`renderRoster()` is the render entry point called after every filter change, edit, delete,
push and pull. A new or changed panel that must track the filter has to be called from
`renderRoster()` (including its empty-roster early-return path); a panel that only paints on
first load is a violation.

# Roster text is escaped before it reaches innerHTML
paths: index.html
Character `name`, `player` and any other user-entered string interpolated into an HTML string
must be escaped first. Interpolating raw field values into `innerHTML` is an injection hole,
because those values are typed into a form and synced from a shared JSON file.

# localStorage keys keep their names and shape
paths: index.html
Persisted state lives under exactly two keys: `wow_forever_roster` (the character array) and
`wow_gh_config` (`owner`, `repo`, `branch`, `token`). Storing the array under a new key,
changing the shape of either value, or reading one without a fallback to the defaults is a
violation.

# The page works with no GitHub configuration
paths: index.html
With no `owner`/`repo` set the app runs local-only: roster comes from `localStorage` or
`DEFAULT_CHARACTERS`, saves write through to `localStorage`, and the status line says so.
Making a feature depend on a successful API call, or letting a failed fetch/push blank or
corrupt the roster instead of falling back and reporting status, is a violation.

# GitHub tokens stay out of files and out of logs
paths: index.html
The PAT is entered by the user into the Settings modal and kept in browser `localStorage`; it
is sent only to `api.github.com`. Committing a token, embedding a default, putting one in
`characters.json` or a commit message, or logging it is a violation.

# characters.json is machine-owned
paths: characters.json
The tracker commits `characters.json` with `[skip ci]`, so its contents and formatting are
produced elsewhere. A UI or code change must not hand-edit, reformat, reorder, or silently
rewrite that file; if the data itself needs to change, say so and ask instead.

# Keep the existing visual language
paths: index.html
Sections are `wow-border bg-slate-900/70 backdrop-blur rounded-xl` panels with a `cinzel`
amber heading, `text-[10px] uppercase tracking-wider` labels, slate borders and the class
colour tokens already defined in `CLASS_COLORS`. Reaching for inline `style` colours, a new
typography stack, or a light theme breaks the one look this site has.

# Verify the change deterministically before claiming it is done
Every change ships with a check that actually ran: parse `characters.json` with a JSON parser
and confirm the invariants above, and cross-check that every `getElementById` target and every
`on*="fn("` handler in `index.html` resolves. Claiming a change works without running a check,
or checking only that the file was written, is a violation.
