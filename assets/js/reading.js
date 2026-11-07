// Fills the reading progress bar, and remembers each reader's chapter in a serial.
// The chapter is kept only in the reader's own browser.
(() => {
  const article = document.querySelector(".reading");
  const text = article.querySelector(".prose");
  const progressBar = document.querySelector(".reading-progress");
  const resume = document.querySelector(".resume");
  // data-work names the serial, and data-chapter is set only on chapter pages.
  const { work, chapter } = article.dataset;
  const storageKey = `reading:${work}`;

  function readSavedChapter() {
    try {
      return localStorage.getItem(storageKey);
    } catch {
      return null; // Storage can be blocked, and resuming is optional.
    }
  }

  function saveChapter() {
    try {
      localStorage.setItem(storageKey, chapter);
    } catch {
      // Storage can be blocked, and resuming is optional.
    }
  }

  function showResumeLink() {
    const chapterPath = readSavedChapter();
    const chapterLinks = [...document.querySelectorAll(".fiction-list a")];
    const chapterLink = chapterLinks.find(
      (link) => link.getAttribute("href") === chapterPath,
    );
    if (!chapterLink) return;

    const resumeLink = resume.querySelector("a");
    resumeLink.href = chapterPath;
    resumeLink.textContent = `從上次的章節繼續：${chapterLink.textContent}`;
    resume.hidden = false;
  }

  function updateProgress() {
    // Text counts as read once the bottom of the window passes it.
    const { top, height } = text.getBoundingClientRect();
    const fractionRead = (window.innerHeight - top) / height;
    const clamped = Math.min(Math.max(fractionRead, 0), 1);
    progressBar.style.transform = `scaleX(${clamped})`;
  }

  // pageshow also covers Back restoring this page without rerunning the script.
  if (resume) window.addEventListener("pageshow", showResumeLink);

  if (chapter) saveChapter();

  if (progressBar) {
    updateProgress();
    window.addEventListener("scroll", updateProgress, { passive: true });
    window.addEventListener("resize", updateProgress);
  }
})();
