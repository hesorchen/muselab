// Pure parsing only. The page sanitizes Markdown output before inserting it.

// highlight.js core ships ~30 common grammars (~125 KB). The few extras
// muselab vendors as separate files are fetched ONLY when a fenced block
// explicitly names one of them, so a prose-only session never downloads
// them. The names are mapped through an allowlist: `language` comes from the
// rendered markdown class attribute and must never be turned into an
// arbitrary importScripts() path.
const HLJS_LANG_PACKS = {
  dockerfile: "dockerfile", docker: "dockerfile",
  makefile: "makefile", mk: "makefile", mak: "makefile", make: "makefile",
  protobuf: "protobuf", proto: "protobuf",
  accesslog: "accesslog",
  graphql: "graphql", gql: "graphql",
};

function ensureHljsLanguage(language) {
  if (!language || self.hljs.getLanguage(language)) return;
  const pack = HLJS_LANG_PACKS[String(language).toLowerCase()];
  if (!pack) return;
  try {
    importScripts("vendor/hljs-langs/" + pack + ".min.js" + self.location.search);
  } catch (_) { /* a missing pack degrades to auto-detect, never breaks render */ }
}

self.onmessage = ({ data }) => {
  try {
    if (data.kind === "markdown" || data.kind === "markdown-blocks") {
      if (!self.marked) importScripts("vendor/marked.min.js" + self.location.search);
    } else if (data.kind === "highlight") {
      if (!self.hljs) importScripts("vendor/highlight.min.js" + self.location.search);
      ensureHljsLanguage(data.language);
    } else throw new Error("unsupported render operation");
    // Imports can wait on a slow network. Start the page's short CPU deadline
    // only after the required library is available, for every job kind.
    // Already-open pages expect their first response to contain HTML. Only
    // send lifecycle packets to callers that explicitly support them.
    if (data.reportParsing === true) self.postMessage({ parsing: true });
    let html;
    if (data.kind === "markdown") {
      html = self.marked.parse(data.text);
    } else if (data.kind === "markdown-blocks") {
      const tokens = self.marked.lexer(data.text);
      html = [];
      let group = [], size = 0;
      // Raw HTML may open/close across Markdown tokens; preserve that nesting.
      const keepTogether = tokens.some(token => token.type === "html");
      const flush = () => {
        if (!group.length) return;
        group.links = tokens.links;
        html.push({ source: group.map(token => token.raw).join(""),
          html: self.marked.parser(group) });
        group = []; size = 0;
      };
      for (const token of tokens) {
        group.push(token);
        size += token.raw.length;
        if (!keepTogether && size >= 8192) flush();
      }
      flush();
    } else if (data.kind === "highlight") {
      const languages = data.languages.filter(lang => self.hljs.getLanguage(lang));
      html = data.language && self.hljs.getLanguage(data.language)
        ? self.hljs.highlight(data.text, { language: data.language, ignoreIllegals: true }).value
        : self.hljs.highlightAuto(data.text, languages).value;
    } else throw new Error("unsupported render operation");
    self.postMessage({ html });
  } catch (_) { self.postMessage({ failed: true }); }
};
