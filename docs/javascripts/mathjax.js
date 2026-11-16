// MathJax configuration for the pages that pymdownx.arithmatex marks with the
// "arithmatex" class. The MathJax script (vendor/mathjax/tex-svg.js) loads after
// this file. It draws formulas as SVG, so it needs no font files.
window.MathJax = {
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: true,
  },
  svg: {
    fontCache: "global",
  },
  options: {
    ignoreHtmlClass: ".*|",
    processHtmlClass: "arithmatex",
  },
};

// Instant navigation replaces the page content without a reload, so typeset
// each new page again.
document$.subscribe(() => {
  MathJax.startup.output.clearCache();
  MathJax.typesetClear();
  MathJax.texReset();
  MathJax.typesetPromise();
});
