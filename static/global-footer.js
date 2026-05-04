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
          <a href="/cv-score-checker">CV Score Checker</a>
          <a href="/ats-cv-checker">ATS CV Checker</a>
          <a href="/cv-job-description-match">CV Job Description Match</a>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Resources</div>
          <a href="/10-second-cv-test">10-Second CV Test</a>
          <a href="/why-is-my-cv-not-getting-interviews">Why Is My CV Not Getting Interviews?</a>
          <a href="/cv-mistakes-that-cost-interviews">CV Mistakes That Cost Interviews</a>
          <a href="/best-cv-format-for-ats">Best CV Format for ATS</a>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Trust</div>
          <a href="/how-it-works">How it works</a>
          <a href="/faq">FAQ</a>
          <a href="/privacy">Privacy</a>
          <a href="/terms">Terms</a>
          <a href="/about">About</a>
        </div>
      </div>
      <div class="site-footer-bottom">
        <span>© 2026 CV Optimiser</span>
        <span>Secure • Private • No CV storage</span>
      </div>
    </footer>
  `;
}

document.addEventListener("DOMContentLoaded", function() {
  const footerRoot = document.getElementById("siteFooter");
  if (!footerRoot) return;
  footerRoot.innerHTML = renderFooter();
});
