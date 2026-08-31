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
          <a href="/best-free-cv-checker-uk">Best free CV checker UK</a>
          <a href="/ats-cv-checker">ATS CV Checker</a>
          <a href="/cv-score-checker">CV Score Checker</a>
          <a href="/cv-keyword-optimiser">CV Keyword Optimiser</a>
          <a href="/job-description-cv-match">Job Description CV Match</a>
          <a href="/graduate-cv-checker">Graduate CV Checker</a>
          <a href="/sales-cv-checker">Sales CV Checker</a>
          <a href="/project-manager-cv-checker">Project Manager CV Checker</a>
          <a href="/retail-manager-cv-checker">Retail Manager CV Checker</a>
          <a href="/nhs-admin-cv-checker">NHS Admin CV Checker</a>
          <a href="/admin-cv-checker">Admin CV Checker</a>
          <a href="/it-helpdesk-cv-checker">IT Helpdesk CV Checker</a>
          <a href="/customer-success-cv-checker">Customer Success CV Checker</a>
          <a href="/career-change-cv-checker">Career Change CV Checker</a>
        </div>
        <div class="site-footer-links-group">
          <div class="site-footer-title">Resources</div>
          <a href="/guides">Guides</a>
          <a href="/example-cv-report">Example Report</a>
          <a href="/sales-cv-example-report">Sales CV Example</a>
          <a href="/account-manager-cv-example-report">Account Manager Example</a>
          <a href="/project-manager-cv-example-report">Project Manager Example</a>
          <a href="/retail-manager-cv-example-report">Retail Manager Example</a>
          <a href="/admin-cv-example-report">Admin Example</a>
          <a href="/it-helpdesk-cv-example-report">IT Helpdesk Example</a>
          <a href="/customer-success-cv-example-report">Customer Success Example</a>
          <a href="/career-change-cv-example-report">Career Change Example</a>
          <a href="/how-it-works">How it works</a>
          <a href="/how-cv-optimiser-scores-your-cv">How scoring works</a>
          <a href="/cv-optimiser-vs-jobscan">CV Optimiser vs Jobscan</a>
          <a href="/best-ats-cv-checker-uk">Best ATS CV Checker UK</a>
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
