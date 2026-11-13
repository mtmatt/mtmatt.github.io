// Fills the reading progress bar, and remembers each reader's chapter in a serial.
// The chapter is kept only in the reader's own browser, as are the chapters passed
// on the way and the endings reached, which an ending's statistics use. Those
// leave the browser only if the reader adds them to the statistics.
(() => {
  const article = document.querySelector(".reading");
  const text = article.querySelector(".prose");
  const progressBar = document.querySelector(".reading-progress");
  const resume = document.querySelector(".resume");
  const endingStats = document.querySelector(".ending-stats");
  // data-work names the serial, and data-chapter is set only on chapter pages.
  const { work, chapter } = article.dataset;
  const storageKey = `reading:${work}`;
  const trailKey = `trail:${work}`;
  const reachedKey = `endings:${work}`;
  const sentKey = `endings-sent:${work}`;

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

  // Saved JSON of the expected kind, or the fallback if storage is blocked or the
  // value is missing or of another kind.
  function readJSON(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key));
      const sameKind = Array.isArray(value) === Array.isArray(fallback);
      return value && typeof value === "object" && sameKind ? value : fallback;
    } catch {
      return fallback;
    }
  }

  function writeJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage can be blocked, and the statistics manage without it.
    }
  }

  // The chapters passed on the way here, in order. Coming back to one drops what
  // followed it, which covers rereading and starting over. Going on from an
  // ending starts a new trail, because that read-through is over. Only the
  // previous page is checked for being an ending: this page is already recorded
  // as reached when it's one, before pageshow calls this.
  // Each visit keeps its trail in its history entry, so Back, Forward and reload
  // bring back the trail as it was, not one rebuilt from wherever the reader went.
  function recordTrail() {
    const saved = history.state?.trail;
    if (Array.isArray(saved)) {
      writeJSON(trailKey, saved);
      return;
    }
    const trail = readJSON(trailKey, []);
    const seen = trail.indexOf(chapter);
    const afterEnding = Object.hasOwn(readJSON(reachedKey, {}), trail.at(-1));
    let updated;
    if (seen >= 0) updated = trail.slice(0, seen + 1);
    else if (afterEnding) updated = [chapter];
    else updated = [...trail, chapter];
    writeJSON(trailKey, updated);
    history.replaceState({ ...history.state, trail: updated }, "");
  }

  function showResumeLink() {
    const chapterPath = readSavedChapter();
    if (!chapterPath) return;

    const chapterLinks = [...document.querySelectorAll(".fiction-list a")];
    const chapterLink = chapterLinks.find(
      (link) => link.getAttribute("href") === chapterPath,
    );
    const resumeLink = resume.querySelector("a");
    resumeLink.href = chapterPath;
    // A chapter off the main route isn't listed, so its title isn't on this page.
    resumeLink.textContent = chapterLink
      ? `從上次的章節繼續：${chapterLink.textContent}`
      : "從上次的章節繼續";
    resume.hidden = false;
  }

  // An ending's distribution. Pressing the button first asks whether to add this
  // reader's ending, and shows the distribution either way. The ending is sent
  // at most once from this browser.
  function setUpEndingStats() {
    const { api, ending } = endingStats.dataset;
    const show = endingStats.querySelector(".ending-stats-show");
    const ask = endingStats.querySelector(".ending-stats-ask");
    const result = endingStats.querySelector(".ending-stats-result");
    const url = `${api}/endings?work=${encodeURIComponent(work)}`;

    const reached = readJSON(reachedKey, {});
    reached[chapter] = ending;
    writeJSON(reachedKey, reached);

    async function load(request, note) {
      result.textContent = "讀取中……";
      try {
        const response = await request();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json(), note);
        return true;
      } catch {
        result.textContent = "暫時無法取得統計，請稍後再試。";
        show.hidden = false;
        return false;
      }
    }

    function render({ endings, total }, note) {
      const caption = document.createElement("p");
      caption.textContent = total
        ? `共 ${total} 位讀者加入統計。`
        : "還沒有讀者加入統計。";
      if (note) caption.textContent += note;
      const list = document.createElement("ol");
      for (const { ending: endingChapter, count } of endings) {
        const share = total ? Math.round((count / total) * 100) : 0;
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = reached[endingChapter] ?? "？？？";
        if (endingChapter === chapter) name.textContent += "（你的結局）";
        const bar = document.createElement("span");
        bar.className = "ending-stats-bar";
        bar.style.setProperty("--share", `${share}%`);
        const figure = document.createElement("span");
        figure.textContent = `${share}%（${count}）`;
        item.append(name, bar, figure);
        list.append(item);
      }
      result.replaceChildren(caption, list);
    }

    show.addEventListener("click", () => {
      show.hidden = true;
      if (readJSON(sentKey, []).includes(chapter)) {
        load(() => fetch(url), "你已經加入過這個結局。");
      } else {
        ask.hidden = false;
      }
    });

    ask.addEventListener("submit", async (event) => {
      event.preventDefault();
      ask.hidden = true;
      if (event.submitter?.value !== "add") {
        load(() => fetch(url));
        return;
      }
      const reading = { work, ending: chapter };
      // Without storage there's no trail, and the ending still counts without it.
      const trail = readJSON(trailKey, []);
      if (ask.elements.path.checked && trail.at(-1) === chapter)
        reading.path = trail;
      const sent = await load(() =>
        fetch(`${api}/endings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(reading),
        }),
      );
      if (sent) writeJSON(sentKey, [...readJSON(sentKey, []), chapter]);
    });

    endingStats.hidden = false;
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

  if (chapter) {
    saveChapter();
    // pageshow, because Back can restore a page without rerunning this script.
    window.addEventListener("pageshow", recordTrail);
  }

  if (endingStats) setUpEndingStats();

  if (progressBar) {
    updateProgress();
    window.addEventListener("scroll", updateProgress, { passive: true });
    window.addEventListener("resize", updateProgress);
  }
})();
