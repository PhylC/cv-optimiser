function renderFooter() {
  return `
    <footer class="site-footer">
      <div class="site-footer-grid">
        <div class="site-footer-brand">
          <div class="site-footer-title">CV Optimiser</div>
          <p>Fast, practical CV feedback for job applications</p>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Tools</div>
          <a href="/cv-checker">CV Checker</a>
          <a href="/ats-cv-checker">ATS CV Checker</a>
          <a href="/cv-score-checker">CV Score Checker</a>
          <a href="/cv-keyword-optimiser">CV Keyword Optimiser</a>
          <a href="/job-description-cv-match">Job Description CV Match</a>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Resources</div>
          <a href="/guides">Guides</a>
          <a href="/example-cv-report">Example Report</a>
          <a href="/how-it-works">How it works</a>
          <a href="/why-is-my-cv-not-getting-interviews">Why Your CV May Not Be Getting Responses</a>
          <a href="/best-cv-format-uk">Best CV Format UK</a>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Trust</div>
          <a href="/faq">FAQ</a>
          <a href="/pricing">Pricing</a>
          <a href="/privacy">Privacy Policy</a>
          <a href="/terms">Terms</a>
          <a href="/contact">Contact</a>
          <a href="/about">About</a>
        </div>
      </div>
      <div class="site-footer-bottom">
        <span>© 2026 CV Optimiser</span>
        <span>Secure • Private • CV handling explained</span>
      </div>
    </footer>
  `;
}

document.addEventListener("DOMContentLoaded", function() {
  const footerRoot = document.getElementById("siteFooter");
  if (!footerRoot) return;
  footerRoot.innerHTML = renderFooter();
});
