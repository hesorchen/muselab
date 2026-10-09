# Maintainer frontend dependency updates

Users run the committed `frontend/vendor/` files. Node/npm and esbuild are
needed only when maintainers refresh the Mermaid/KaTeX bundle; the application
never downloads these dependencies at runtime.

The pinned `package-lock.json` records npm archive integrity, including
transitive dependencies and the esbuild tool. `entry.mjs` exposes the same
`window.mermaid` interface used by the lazy loader. DOMPurify is overridden to
3.4.16 in Mermaid's dependency graph as well as shipped directly. KaTeX is pinned
to 0.18.2 and overridden to the same version inside Mermaid to retain the
GHSA-238p-pmpm-9mq7 fix in both rendering paths.

From the repository root:

```sh
python scripts/vendor/build.py
python scripts/check-vendor.py
python scripts/check-vendor.py --audit
```

The build uses an isolated temporary directory and `npm ci --ignore-scripts`,
then creates one local IIFE bundle with inline upstream notices. It copies
KaTeX's matching JS/CSS/fonts, inventories each package version contributing
bytes to the Mermaid output, and copies full package licenses/notices. It
also refreshes asset and build-input SHA-256 hashes in
`frontend/vendor/manifest.json`. `--built-dir` reuses an already-built directory
only for maintainer validation and checks its entry/lock/package inputs;
normal refreshes should use a clean build.

When updating pins, review upstream release/security notes, refresh the lock,
update the direct version declarations in the manifest, rebuild, and run the
browser Markdown/math/diagram/security tests. Other directly vendored libraries
remain separate upstream artifacts: preserve their full license texts and
refresh their exact version, source and digest records when replacing them.

`check-vendor.py` is offline by default and fails on missing, extra or changed
assets, changed build inputs, or missing license texts. `--audit` submits only
public npm package names and versions to the OSV advisory service and fails on
unreviewed advisories or unavailable/incomplete scan results. Reviewed version
matches are printed explicitly and bound to an exact bundle hash. A clean scan means no
advisory matched the inventoried versions at scan time; it is not proof that
all dependency behavior is safe.

## Reviewed version matches in the current bundle

The upstream `@mermaid-js/parser@1.2.1` package already contains prebundled
libraries. `embedded.py` inspects source maps for the parser chunks actually
included by esbuild, inventories their exact package versions, and retrieves
full license texts from integrity-verified npm archives. This matters because
an npm lock or esbuild metadata alone does not expose those inner libraries.

The parser includes 227 source modules from lodash-es 4.17.23. Version scanners
correctly match
[GHSA-f23m-r3pf-42rh](https://github.com/lodash/lodash/security/advisories/GHSA-f23m-r3pf-42rh)
and
[GHSA-r5fr-rjxr-66jc](https://github.com/lodash/lodash/security/advisories/GHSA-r5fr-rjxr-66jc),
both fixed in 4.18.0. Their affected `unset.js`, `omit.js`, `_baseUnset.js` and
`template.js` modules are absent from this parser source map; corresponding
function definitions are also absent from the upstream chunk. The full current
lodash-es used elsewhere in the bundle is 4.18.1.

`manifest.json` records the 227-module list, upstream chunk/map hashes, review
links and the exact final bundle SHA-256. `--audit` prints both known version
matches and their source-exclusion rationale. These are narrowly reviewed
exclusions, not a claim that lodash-es 4.17.23 is safe in other applications.
A changed bundle hash, newly included affected module, different map, another
use of that old version, or a new advisory fails the check until reviewed.

One upstream source fragment is identified only as path-browserify's Node.js
v8.11.1 POSIX path implementation. The map supplies no exact npm version. Its
source hash and full MIT notice are recorded, and `--audit` explicitly lists
it as not version-scannable. The npm results therefore do not represent complete
coverage of opaque upstream source fragments.
