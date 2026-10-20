const I18N_MESSAGES = (() => {
    try {
        return JSON.parse(document.getElementById("i18nMessages")?.textContent || "{}");
    } catch {
        return {};
    }
})();
const UI_LOCALE = document.documentElement.lang === "en" ? "en-GB" : "vi-VN";

// English text is the key; the page embeds Vietnamese translations when that language is active.
function t(message, values = {}) {
    const text = I18N_MESSAGES[message] || message;
    return text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
}

function showToast(message, tone = "success") {
    const area = document.getElementById("toastArea");
    if (!area) return;

    const toast = document.createElement("div");
    toast.className = `toast align-items-center text-bg-${tone} border-0`;
    toast.setAttribute("role", "alert");
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body"></div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="${escapeHtml(t("Close"))}"></button>
        </div>
    `;
    // Messages can contain YouTube titles, ID3 tags or yt-dlp output; never parse them as HTML.
    toast.querySelector(".toast-body").textContent = String(message ?? "");
    area.appendChild(toast);
    const instance = new bootstrap.Toast(toast, { delay: 3500 });
    instance.show();
    toast.addEventListener("hidden.bs.toast", () => toast.remove());
}

function readCookie(name) {
    const prefix = `${name}=`;
    return document.cookie
        .split(";")
        .map((item) => item.trim())
        .find((item) => item.startsWith(prefix))
        ?.slice(prefix.length) || "";
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, {
            ...options,
            signal: options.signal || controller.signal,
        });
    } finally {
        window.clearTimeout(timeout);
    }
}

async function requestJson(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const { timeoutMs = 12000, headers: optionHeaders = {}, ...fetchOptions } = options;
    const csrfCookieName = document.querySelector('meta[name="csrf-cookie-name"]')?.content || "ytarchive_csrf";
    const csrfHeaderName = document.querySelector('meta[name="csrf-header-name"]')?.content || "x-csrf-token";
    const csrfToken = method === "GET" ? "" : readCookie(csrfCookieName);
    const response = await fetchWithTimeout(url, {
        headers: {
            "Content-Type": "application/json",
            ...(csrfToken ? { [csrfHeaderName]: decodeURIComponent(csrfToken) } : {}),
            ...optionHeaders,
        },
        ...fetchOptions,
    }, timeoutMs);
    if (response.status === 401) {
        if (!isLoggingOut) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        }
        throw new Error(t("Authentication required"));
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || data.message || t("Request failed with {status}", { status: response.status }));
    }
    return data;
}

const YTDLP_VERSION_STORAGE_KEY = "ytarchiveYtDlpVersion";
let ytDlpStatusRefreshing = false;

function formatLocalTimestamp(value) {
    if (!value) return null;
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) return value;
    return timestamp.toLocaleString(UI_LOCALE, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    });
}

function renderYtDlpStatus(status) {
    const version = document.getElementById("ytDlpInstalledVersion");
    const previous = document.getElementById("ytDlpPreviousVersion");
    const result = document.getElementById("ytDlpUpdateResult");
    const lastCheck = document.getElementById("ytDlpLastCheck");
    const lastUpdated = document.getElementById("ytDlpLastUpdated");
    const error = document.getElementById("ytDlpUpdateError");
    if (!version) return;

    version.textContent = status.installed_version || t("Not installed");
    previous.textContent = status.previous_version
        ? t("Previous version: {version}", { version: status.previous_version })
        : t("No previous version");
    result.textContent = status.last_error ? t("Check failed") : t("Ready");
    result.closest(".status-dot-label")?.classList.toggle("is-error", Boolean(status.last_error));
    lastCheck.textContent = status.last_successful_check_at
        ? t("Last check: {time}", { time: formatLocalTimestamp(status.last_successful_check_at) })
        : t("Not checked yet");
    lastUpdated.textContent = status.last_updated_at
        ? t("Last updated: {time}", { time: formatLocalTimestamp(status.last_updated_at) })
        : t("No update has completed yet.");
    if (status.last_error) {
        error.querySelector("span").textContent = status.last_error;
        error.classList.remove("d-none");
    } else {
        error.classList.add("d-none");
    }
}

async function refreshYtDlpStatus() {
    if (!hasAuthenticatedShell() || ytDlpStatusRefreshing || isLoggingOut) return null;
    ytDlpStatusRefreshing = true;
    try {
        const status = await requestJson("/api/yt-dlp/status", { timeoutMs: 3000 });
        renderYtDlpStatus(status);
        const currentVersion = status.installed_version;
        const previousSeenVersion = window.localStorage.getItem(YTDLP_VERSION_STORAGE_KEY);
        if (currentVersion && previousSeenVersion && currentVersion !== previousSeenVersion) {
            showToast(t("yt-dlp updated: {from} → {to}", { from: previousSeenVersion, to: currentVersion }));
        }
        if (currentVersion) window.localStorage.setItem(YTDLP_VERSION_STORAGE_KEY, currentVersion);
        return status;
    } catch (error) {
        console.warn("Unable to refresh yt-dlp status", error);
        return null;
    } finally {
        ytDlpStatusRefreshing = false;
    }
}

async function uploadMp3File(playlistId, file) {
    const csrfCookieName = document.querySelector('meta[name="csrf-cookie-name"]')?.content || "ytarchive_csrf";
    const csrfHeaderName = document.querySelector('meta[name="csrf-header-name"]')?.content || "x-csrf-token";
    const csrfToken = readCookie(csrfCookieName);
    const response = await fetchWithTimeout(`/playlist/${playlistId}/videos/upload`, {
        method: "POST",
        headers: {
            "Content-Type": "audio/mpeg",
            "X-File-Name": encodeURIComponent(file.name),
            ...(csrfToken ? { [csrfHeaderName]: decodeURIComponent(csrfToken) } : {}),
        },
        body: file,
    }, 10 * 60 * 1000);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || t("Upload failed with {status}", { status: response.status }));
    return data;
}

const pageCache = new Map();
const pagePrefetches = new Map();
const PAGE_CACHE_TTL_MS = 30_000;
const PAGE_CACHE_MAX_ENTRIES = 16;
const PAGE_PREFETCH_LIMIT = 8;
const PAGE_PREFETCH_START_DELAY_MS = 180;
let isLoggingOut = false;

function navigationKey(url) {
    const normalized = new URL(url, window.location.href);
    normalized.hash = "";
    return `${normalized.pathname}${normalized.search}`;
}

// Settings queries Windows Task Scheduler through PowerShell, so it is never fetched speculatively.
function isPrefetchableUrl(url) {
    const nextUrl = new URL(url, window.location.href);
    return isCacheableNavigationUrl(nextUrl) && !nextUrl.pathname.startsWith("/settings");
}

function isCacheableNavigationUrl(url) {
    const nextUrl = new URL(url, window.location.href);
    if (nextUrl.origin !== window.location.origin) return false;
    if (nextUrl.pathname.startsWith("/stream/")) return false;
    if (nextUrl.pathname.startsWith("/task/")) return false;
    if (nextUrl.pathname.startsWith("/api/")) return false;
    if (nextUrl.pathname === "/login") return false;
    return true;
}

function hasAuthenticatedShell() {
    return Boolean(document.getElementById("activeDownloads"));
}

function trimPageCache() {
    while (pageCache.size > PAGE_CACHE_MAX_ENTRIES) {
        pageCache.delete(pageCache.keys().next().value);
    }
}

function invalidatePageCache() {
    pageCache.clear();
    // Called after every change to playlists or songs, so the song search must reload too.
    invalidatePlaylistSearchIndex();
}

function cachedPage(url) {
    const key = navigationKey(url);
    const snapshot = pageCache.get(key);
    if (!snapshot) return null;
    if (Date.now() - snapshot.fetchedAt > PAGE_CACHE_TTL_MS) {
        pageCache.delete(key);
        return null;
    }
    return snapshot;
}

function storePageSnapshot(snapshot) {
    pageCache.set(snapshot.key, snapshot);
    trimPageCache();
}

function storeCurrentPageSnapshot() {
    const currentContent = document.getElementById("pageContent");
    if (!currentContent || !hasAuthenticatedShell() || !isCacheableNavigationUrl(window.location.href)) return;
    storePageSnapshot({
        key: navigationKey(window.location.href),
        finalUrl: window.location.href,
        title: document.title,
        headerHtml: document.getElementById("shellHeader")?.outerHTML || "",
        contentHtml: currentContent.innerHTML,
        fetchedAt: Date.now(),
    });
}

function buildPageSnapshot(response, html, requestedUrl) {
    const finalUrl = new URL(response.url || requestedUrl.href, window.location.href);
    if (finalUrl.pathname === "/login") {
        return { redirectUrl: finalUrl.href };
    }

    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    const nextContent = nextDocument.getElementById("pageContent");
    if (!nextContent) return null;

    return {
        key: navigationKey(finalUrl),
        finalUrl: finalUrl.href,
        title: nextDocument.title || document.title,
        headerHtml: nextDocument.getElementById("shellHeader")?.outerHTML || "",
        contentHtml: nextContent.innerHTML,
        fetchedAt: Date.now(),
    };
}

async function fetchPageSnapshot(url, options = {}) {
    const { force = false, navigateOnAuthRedirect = false } = options;
    const nextUrl = new URL(url, window.location.href);
    if (!isCacheableNavigationUrl(nextUrl)) return null;

    const key = navigationKey(nextUrl);
    if (!force) {
        const existing = cachedPage(nextUrl);
        if (existing) return existing;
        if (pagePrefetches.has(key)) return pagePrefetches.get(key);
    }

    const promise = (async () => {
        const response = await fetchWithTimeout(nextUrl.href, {
            credentials: "same-origin",
            headers: { "X-Requested-With": "fetch" },
        }, 15000);
        if (!response.ok) {
            throw new Error(`Navigation failed with ${response.status}`);
        }
        const html = await response.text();
        const snapshot = buildPageSnapshot(response, html, nextUrl);
        if (snapshot?.redirectUrl) {
            if (navigateOnAuthRedirect) {
                window.location.href = snapshot.redirectUrl;
            }
            return null;
        }
        if (snapshot) storePageSnapshot(snapshot);
        return snapshot;
    })().finally(() => {
        pagePrefetches.delete(key);
    });

    pagePrefetches.set(key, promise);
    return promise;
}

function scheduleActiveDownloadsRefresh(delay = 450) {
    if (!hasAuthenticatedShell()) return;
    window.setTimeout(() => {
        refreshActiveDownloads()
            .then((tasks) => scheduleActiveDownloadsPoll(tasks.length ? 1500 : 8000))
            .catch(() => scheduleActiveDownloadsPoll(8000));
    }, delay);
}

function renderPageSnapshot(snapshot, options = {}) {
    const { push = true, scrollToTop = true } = options;
    const currentContent = document.getElementById("pageContent");
    if (!snapshot || !currentContent) return false;

    document.title = snapshot.title || document.title;
    const currentHeader = document.getElementById("shellHeader");
    if (currentHeader && snapshot.headerHtml) currentHeader.outerHTML = snapshot.headerHtml;
    currentContent.innerHTML = snapshot.contentHtml;
    if (push) {
        window.history.pushState({ softNavigation: true }, "", snapshot.finalUrl);
    }
    syncNavigationState();
    initPlaylistTools();
    storeCurrentPageSnapshot();
    scheduleActiveDownloadsRefresh();
    if (scrollToTop) window.scrollTo(0, 0);
    return true;
}

function linkFromEvent(event) {
    return event.target?.closest ? event.target.closest("a") : null;
}

function prefetchNavigationUrl(url, options = {}) {
    if (!hasAuthenticatedShell() || isLoggingOut) return Promise.resolve(null);
    const nextUrl = new URL(url, window.location.href);
    if (!isPrefetchableUrl(nextUrl)) return Promise.resolve(null);
    if (navigationKey(nextUrl) === navigationKey(window.location.href)) return Promise.resolve(null);
    return fetchPageSnapshot(nextUrl, options).catch((error) => {
        console.debug("Navigation prefetch skipped", error);
        return null;
    });
}

function stopBackgroundNavigationWork() {
    isLoggingOut = true;
    invalidatePageCache();
    if (activeDownloadsPollTimer) {
        window.clearTimeout(activeDownloadsPollTimer);
        activeDownloadsPollTimer = null;
    }
    pagePrefetches.clear();
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function taskProgress(task) {
    return Math.max(0, Math.min(100, Number(task.progress || 0)));
}

function taskCountsText(task) {
    const total = Number(task.total || 0);
    const completed = Number(task.completed || 0);
    const failed = Number(task.failed || 0);
    const processed = completed + failed;
    if (!total) return t("Preparing");
    return t("{processed}/{total} processed | {completed} downloaded | {failed} failed", { processed, total, completed, failed });
}

const TASK_STATUS_LABELS = { queued: "queued", running: "running", done: "done", failed: "failed" };

function taskStatusLabel(task) {
    if (task.status === "done" && Number(task.failed || 0) > 0) return t("done with errors");
    return t(TASK_STATUS_LABELS[task.status] || task.status);
}

function renderActiveDownloads(tasks) {
    const panel = document.getElementById("activeDownloads");
    const list = document.getElementById("activeDownloadsList");
    if (!panel || !list) return;

    if (!tasks.length) {
        panel.classList.add("d-none");
        list.innerHTML = "";
        return;
    }

    panel.classList.remove("d-none");
    list.innerHTML = tasks.map((task) => {
        const progress = taskProgress(task);
        const playlistTitle = task.playlist_title || t("Playlist {id}", { id: task.playlist_id || "" });
        const current = task.current_video ? ` | ${escapeHtml(task.current_video)}` : "";
        const tone = task.status === "failed" || Number(task.failed || 0) > 0 ? "is-warning" : "";
        const errors = Array.isArray(task.errors) ? task.errors : [];
        const visibleErrors = errors.slice(0, 5);
        const hiddenErrorCount = Math.max(0, errors.length - visibleErrors.length);
        const errorHtml = visibleErrors.length ? `
            <div class="active-download-errors">
                <div class="active-download-errors-title">
                    <i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(t("Failed videos"))}
                </div>
                ${visibleErrors.map((item) => `
                    <div class="active-download-error-item">
                        <strong>${escapeHtml(item.title || t("Unknown video"))}</strong>
                        <span class="text-muted">(${escapeHtml(item.yt_video_id || "")})</span>:
                        ${escapeHtml(item.message || t("Download failed"))}
                    </div>
                `).join("")}
                ${hiddenErrorCount ? `<div class="active-download-error-item text-muted">${escapeHtml(t("+{count} more failed video(s)", { count: hiddenErrorCount }))}</div>` : ""}
            </div>
        ` : "";
        return `
            <div class="active-download-item" data-task-id="${escapeHtml(task.task_id)}">
                <div class="active-download-row">
                    <div>
                        <div class="active-download-title">${escapeHtml(playlistTitle)}</div>
                        <div class="active-download-meta">
                            ${escapeHtml(String(task.format || "").toUpperCase())}
                            | ${escapeHtml(taskStatusLabel(task))}
                            | ${escapeHtml(taskCountsText(task))}
                            ${current}
                        </div>
                    </div>
                    <a class="row-icon-btn" href="/playlist/${task.playlist_id}" title="${escapeHtml(t("Open playlist"))}">
                        <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                </div>
                <div class="progress active-download-progress" role="progressbar" aria-label="${escapeHtml(t("Download progress"))}">
                    <div class="progress-bar ${tone}" style="width: ${progress}%"></div>
                </div>
                ${errorHtml}
            </div>
        `;
    }).join("");
}

let activeDownloadsRefreshing = false;
let activeDownloadsPollTimer = null;

async function refreshActiveDownloads() {
    if (!document.getElementById("activeDownloads")) return [];
    if (activeDownloadsRefreshing) return [];

    activeDownloadsRefreshing = true;
    try {
        const tasks = await requestJson("/tasks/active", { timeoutMs: 2500 });
        renderActiveDownloads(tasks);
        return tasks;
    } catch (error) {
        console.warn("Unable to refresh active downloads", error);
        return [];
    } finally {
        activeDownloadsRefreshing = false;
    }
}

function scheduleActiveDownloadsPoll(delay = 8000) {
    if (!document.getElementById("activeDownloads")) return;
    if (activeDownloadsPollTimer) window.clearTimeout(activeDownloadsPollTimer);
    activeDownloadsPollTimer = window.setTimeout(async () => {
        const tasks = await refreshActiveDownloads();
        scheduleActiveDownloadsPoll(tasks.length ? 1500 : 8000);
    }, delay);
}

const POLL_TASK_MAX_FAILURES = 6;

// A busy server can miss one 3 s status request; retry with backoff instead of giving up on the task.
async function pollTask(taskId) {
    let failures = 0;
    for (;;) {
        let task;
        try {
            task = await requestJson(`/task/${taskId}/status`, { timeoutMs: 5000 });
            failures = 0;
        } catch (error) {
            failures += 1;
            if (failures >= POLL_TASK_MAX_FAILURES) {
                showToast(t("Lost track of the task; the page will refresh."), "warning");
                invalidatePageCache();
                refreshCurrentPage();
                return null;
            }
            await new Promise((resolve) => window.setTimeout(resolve, Math.min(15000, 1500 * 2 ** (failures - 1))));
            continue;
        }

        if (task.status === "done") {
            invalidatePageCache();
            if (Number(task.failed || 0) > 0) {
                showToast(t("Sync done: {completed} downloaded, {failed} failed", { completed: task.completed, failed: task.failed }), "warning");
            } else {
                showToast(task.format === "metadata" ? t("Metadata imported") : t("Sync completed"));
            }
            window.setTimeout(() => refreshCurrentPage(), 700);
            refreshYtDlpStatus();
            return task;
        }
        if (task.status === "failed") {
            invalidatePageCache();
            showToast(task.error || t("Sync failed"), "danger");
            window.setTimeout(() => refreshCurrentPage(), 700);
            return task;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
    }
}

const playbackState = {
    pageTracks: [],
    queue: [],
    currentIndex: -1,
    currentTrack: null,
    lastKnownTime: 0,
    shouldResume: false,
    recoveryAttempts: 0,
    recoveryTimer: null,
    isSeeking: false,
};

const MAX_PLAYBACK_RECOVERY_ATTEMPTS = 4;

function loadPlaybackSettings() {
    const defaults = { repeat: false, autoNext: true, shuffle: false };
    try {
        return { ...defaults, ...JSON.parse(window.localStorage.getItem("ytarchivePlaybackSettings") || "{}") };
    } catch {
        return defaults;
    }
}

const playbackSettings = loadPlaybackSettings();

function savePlaybackSettings() {
    try {
        window.localStorage.setItem("ytarchivePlaybackSettings", JSON.stringify(playbackSettings));
    } catch {
        // Playback still works when localStorage is unavailable.
    }
}

function normalizeSearch(value) {
    return String(value || "").trim().toLocaleLowerCase();
}

let playlistSearchIndex = null;
let playlistSearchIndexRequest = null;
let playlistSearchIndexGeneration = 0;

function invalidatePlaylistSearchIndex() {
    playlistSearchIndex = null;
    playlistSearchIndexRequest = null;
    playlistSearchIndexGeneration += 1;
}

function loadPlaylistSearchIndex() {
    if (playlistSearchIndex) return Promise.resolve(playlistSearchIndex);
    if (!playlistSearchIndexRequest) {
        const generation = playlistSearchIndexGeneration;
        playlistSearchIndexRequest = requestJson("/api/playlists/search-index", { timeoutMs: 15000 })
            .then((index) => {
                // Drop a response that started before the data changed.
                if (generation === playlistSearchIndexGeneration) playlistSearchIndex = index;
                return index;
            })
            .catch((error) => {
                console.warn("Unable to load the song search index", error);
                return {};
            })
            .finally(() => {
                if (generation === playlistSearchIndexGeneration) playlistSearchIndexRequest = null;
            });
    }
    return playlistSearchIndexRequest;
}

function updatePlaylistSearch() {
    const input = document.getElementById("playlistSearch");
    const cards = Array.from(document.querySelectorAll(".js-playlist-card"));
    const count = document.getElementById("playlistSearchCount");
    const empty = document.getElementById("playlistSearchEmpty");
    if (!input && !cards.length) return;

    const query = normalizeSearch(input?.value || "");
    if (query && !playlistSearchIndex) {
        // Match playlist names now and song names once the index arrives.
        loadPlaylistSearchIndex().then(() => {
            if (document.getElementById("playlistSearch") === input) updatePlaylistSearch();
        });
    }
    let visibleCount = 0;

    cards.forEach((card) => {
        const playlistText = [card.dataset.title, card.dataset.url].join(" ");
        const songs = query && playlistSearchIndex ? playlistSearchIndex[card.dataset.playlistId] || [] : [];
        const songMatches = songs.filter((song) => (
            normalizeSearch([song.title, song.channel_name, song.source_id].join(" ")).includes(query)
        ));
        const playlistMatches = !query || normalizeSearch(playlistText).includes(query);
        const matches = playlistMatches || songMatches.length > 0;
        card.classList.toggle("d-none", !matches);
        const matchBox = card.querySelector(".dashboard-song-matches");
        if (matchBox) {
            if (query && songMatches.length) {
                const names = songMatches.slice(0, 3).map((song) => song.title);
                const remaining = songMatches.length - names.length;
                matchBox.textContent = t("Matching songs: {names}", { names: names.join(", ") }) + (remaining ? ` (+${remaining})` : "");
                matchBox.classList.remove("d-none");
            } else {
                matchBox.textContent = "";
                matchBox.classList.add("d-none");
            }
        }
        if (matches) visibleCount += 1;
    });

    if (count) {
        count.textContent = query
            ? t("{visible}/{total} playlists", { visible: visibleCount, total: cards.length })
            : t("{count} playlists", { count: cards.length });
    }
    if (empty) empty.classList.toggle("d-none", !query || visibleCount > 0);
}

const LIBRARY_VIEW_STORAGE_KEY = "ytarchive.libraryView";

function readLibraryView() {
    try {
        return window.localStorage.getItem(LIBRARY_VIEW_STORAGE_KEY) === "list" ? "list" : "grid";
    } catch {
        return "grid";
    }
}

function applyLibraryView(view) {
    const grid = document.getElementById("playlistGrid");
    if (!grid) return;
    grid.classList.toggle("is-list", view === "list");
    document.querySelectorAll(".js-library-view").forEach((button) => {
        const active = button.dataset.view === view;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
    });
}

function updateLibrarySearch(input) {
    const query = normalizeSearch(input?.value || "");
    input?.closest(".custom-source-card")?.querySelectorAll(".custom-library-item").forEach((item) => {
        item.classList.toggle("d-none", Boolean(query) && !normalizeSearch(item.dataset.search).includes(query));
    });
}

function updateAutoSyncToggle(input) {
    if (!input) return;
    const toggle = input.closest(".auto-sync-toggle");
    const label = toggle?.querySelector(".js-auto-sync-label");
    if (toggle) toggle.classList.toggle("is-enabled", input.checked);
    if (label) label.textContent = input.checked ? t("Auto-sync: on") : t("Auto-sync: off");
}

function syncAutoSyncToggleLabels() {
    document.querySelectorAll(".js-auto-sync").forEach(updateAutoSyncToggle);
}

function updateVideoSearch() {
    const input = document.getElementById("videoSearch");
    const rows = Array.from(document.querySelectorAll(".js-video-row"));
    const count = document.getElementById("videoSearchCount");
    const empty = document.getElementById("videoSearchEmpty");
    if (!input || !rows.length) return;

    const query = normalizeSearch(input.value);
    let visibleCount = 0;

    rows.forEach((row) => {
        const searchableText = [
            row.dataset.title,
            row.dataset.channel,
            row.dataset.ytId,
        ].join(" ");
        const matches = !query || normalizeSearch(searchableText).includes(query);
        row.classList.toggle("d-none", !matches);
        if (matches) visibleCount += 1;
    });

    if (count) count.textContent = query ? t("{visible}/{total} videos", { visible: visibleCount, total: rows.length }) : t("{count} videos", { count: rows.length });
    if (empty) empty.classList.toggle("d-none", visibleCount > 0);
    syncVisiblePlaybackState();
}

function refreshPlaybackTracks() {
    playbackState.pageTracks = Array.from(document.querySelectorAll(".js-play-media")).map((button) => ({
        id: button.dataset.videoId || button.dataset.streamUrl,
        button,
        row: button.closest(".js-video-row"),
        title: button.dataset.title || t("Untitled track"),
        url: button.dataset.streamUrl,
    })).filter((track) => track.url);
    syncVisiblePlaybackState();
}

function visiblePlaybackTracks() {
    const visible = playbackState.pageTracks.filter((track) => !track.row?.classList.contains("d-none"));
    return visible.length ? visible : playbackState.pageTracks;
}

function toPlayableTrack(track) {
    return {
        id: track.id || track.url,
        title: track.title || t("Untitled track"),
        url: track.url,
    };
}

function trackKey(track) {
    if (!track?.url) return "";
    try {
        const url = new URL(track.url, window.location.origin);
        return `${url.pathname}${url.search}`;
    } catch {
        return track.url;
    }
}

function sameTrack(left, right) {
    return Boolean(left && right && trackKey(left) === trackKey(right));
}

function getPlayerElements() {
    return {
        bar: document.getElementById("globalPlayerBar"),
        media: document.getElementById("globalPlayerMedia"),
        title: document.getElementById("globalPlayerTitle"),
        playPause: document.getElementById("globalPlayerPlayPause"),
        seek: document.getElementById("globalPlayerSeek"),
        currentTime: document.getElementById("globalPlayerCurrentTime"),
        duration: document.getElementById("globalPlayerDuration"),
        volume: document.getElementById("globalPlayerVolume"),
        mute: document.getElementById("globalPlayerMute"),
        shuffle: document.getElementById("globalPlayerShuffle"),
        repeat: document.getElementById("globalPlayerRepeat"),
        autoNext: document.getElementById("globalPlayerAutoNext"),
    };
}

function formatPlaybackTime(value) {
    const seconds = Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = seconds % 60;
    if (hours) {
        return `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
    }
    return `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function setRangeProgress(input, value, max) {
    if (!input) return;
    const safeMax = Number(max) || Number(input.max) || 1;
    const progress = Math.max(0, Math.min(100, (Number(value) / safeMax) * 100));
    input.style.setProperty("--range-progress", `${progress}%`);
}

function updateGlobalPlayerControls() {
    const { media, playPause, seek, currentTime, duration, volume, mute, shuffle, repeat, autoNext } = getPlayerElements();
    if (!media) return;

    if (playPause) {
        playPause.innerHTML = media.paused ? '<i class="bi bi-play-fill"></i>' : '<i class="bi bi-pause-fill"></i>';
        playPause.classList.toggle("is-active", !media.paused);
    }

    const mediaDuration = Number.isFinite(media.duration) ? media.duration : 0;
    // While the user drags, timeupdate must not pull the thumb back to the playing position.
    if (seek && !playbackState.isSeeking) {
        const seekValue = mediaDuration ? Math.round((media.currentTime / mediaDuration) * 1000) : 0;
        seek.value = String(seekValue);
        setRangeProgress(seek, seekValue, 1000);
    }
    if (currentTime) currentTime.textContent = formatPlaybackTime(media.currentTime);
    if (duration) duration.textContent = formatPlaybackTime(mediaDuration);

    if (volume) {
        volume.value = String(media.muted ? 0 : media.volume);
        setRangeProgress(volume, Number(volume.value), 1);
    }
    if (mute) {
        const muted = media.muted || media.volume === 0;
        mute.innerHTML = muted ? '<i class="bi bi-volume-mute-fill"></i>' : '<i class="bi bi-volume-up-fill"></i>';
        mute.classList.toggle("is-active", muted);
    }
    if (shuffle) shuffle.classList.toggle("is-active", Boolean(playbackSettings.shuffle));
    if (repeat) repeat.classList.toggle("is-active", Boolean(playbackSettings.repeat));
    if (autoNext) autoNext.classList.toggle("is-active", Boolean(playbackSettings.autoNext));
    [shuffle, repeat, autoNext].forEach((button) => button?.setAttribute("aria-pressed", String(button.classList.contains("is-active"))));
}

function showGlobalPlayer() {
    const { bar } = getPlayerElements();
    if (!bar) return;
    bar.classList.remove("d-none");
    document.body.classList.add("has-player-bar");
    updateGlobalPlayerControls();
}

function clearPlaybackRecoveryTimer() {
    if (playbackState.recoveryTimer) {
        window.clearTimeout(playbackState.recoveryTimer);
        playbackState.recoveryTimer = null;
    }
}

function isAutoplayPolicyError(error) {
    return error?.name === "NotAllowedError";
}

function playbackRetryUrl(url) {
    const retryUrl = new URL(url, window.location.origin);
    retryUrl.searchParams.set("_playback_retry", String(Date.now()));
    return retryUrl.href;
}

function schedulePlaybackRecovery() {
    if (!playbackState.currentTrack || !playbackState.shouldResume || playbackState.recoveryTimer) return;
    if (playbackState.recoveryAttempts >= MAX_PLAYBACK_RECOVERY_ATTEMPTS) {
        showToast(t("Local media server is unavailable. Please restart the app if playback does not recover."), "danger");
        return;
    }

    if (playbackState.recoveryAttempts === 0) {
        showToast(t("Local media connection was interrupted. Reconnecting..."), "warning");
    }
    const delay = 1200 * (playbackState.recoveryAttempts + 1);
    playbackState.recoveryTimer = window.setTimeout(() => {
        playbackState.recoveryTimer = null;
        recoverCurrentPlayback();
    }, delay);
}

async function recoverCurrentPlayback() {
    const { media } = getPlayerElements();
    const track = playbackState.currentTrack;
    if (!media || !track?.url || !playbackState.shouldResume) return;

    playbackState.recoveryAttempts += 1;
    const resumeAt = playbackState.lastKnownTime;
    media.src = playbackRetryUrl(track.url);
    media.load();

    const restorePosition = () => {
        if (resumeAt > 0 && Number.isFinite(media.duration) && media.duration > 0) {
            media.currentTime = Math.min(resumeAt, Math.max(0, media.duration - 0.25));
        }
    };
    media.addEventListener("loadedmetadata", restorePosition, { once: true });

    try {
        await media.play();
    } catch (error) {
        console.warn("Media playback recovery failed", error);
        if (isAutoplayPolicyError(error)) {
            showToast(t("Press play in the media bar to resume playback"), "warning");
        } else {
            schedulePlaybackRecovery();
        }
    }
}

function stopPlayback() {
    const { bar, media, title, seek, currentTime, duration } = getPlayerElements();
    if (media) {
        media.pause();
        media.removeAttribute("src");
        media.load();
    }
    if (title) title.textContent = t("No track selected");
    if (seek) {
        seek.value = "0";
        setRangeProgress(seek, 0, 1000);
    }
    if (currentTime) currentTime.textContent = "0:00";
    if (duration) duration.textContent = "0:00";
    if (bar) bar.classList.add("d-none");
    document.body.classList.remove("has-player-bar");
    playbackState.currentTrack = null;
    playbackState.currentIndex = -1;
    playbackState.lastKnownTime = 0;
    playbackState.shouldResume = false;
    playbackState.recoveryAttempts = 0;
    clearPlaybackRecoveryTimer();
    syncVisiblePlaybackState();
    updateGlobalPlayerControls();
}

function applyPlaybackSettingsToMedia() {
    const { media } = getPlayerElements();
    if (media) media.loop = playbackSettings.repeat;
    updateGlobalPlayerControls();
}

function syncVisiblePlaybackState() {
    playbackState.pageTracks.forEach((item) => {
        const active = sameTrack(item, playbackState.currentTrack);
        item.row?.classList.toggle("is-playing", active);
        item.button.classList.toggle("is-playing", active);
        item.button.innerHTML = active ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-play-fill"></i>';
    });
}

function resetQueueFromPage(track) {
    const visible = visiblePlaybackTracks();
    const source = visible.some((item) => sameTrack(item, track)) ? visible : playbackState.pageTracks;
    playbackState.queue = source.map(toPlayableTrack);
    playbackState.currentIndex = playbackState.queue.findIndex((item) => sameTrack(item, track));
    if (playbackState.currentIndex < 0) {
        playbackState.queue = [toPlayableTrack(track)];
        playbackState.currentIndex = 0;
    }
}

async function playTrack(track, options = {}) {
    const { resetQueue = true } = options;
    const { media, title } = getPlayerElements();
    if (!track || !track.url || !media || !title) return;

    if (resetQueue) resetQueueFromPage(track);

    const playableTrack = toPlayableTrack(track);
    const changedTrack = !sameTrack(playbackState.currentTrack, playableTrack);
    playbackState.currentTrack = playableTrack;
    playbackState.currentIndex = playbackState.queue.findIndex((item) => sameTrack(item, playableTrack));
    if (playbackState.currentIndex < 0) {
        playbackState.queue = [playableTrack];
        playbackState.currentIndex = 0;
    }

    const absoluteUrl = new URL(playableTrack.url, window.location.origin).href;
    if (changedTrack || media.src !== absoluteUrl || media.error) {
        media.src = playableTrack.url;
    }
    if (changedTrack) playbackState.lastKnownTime = 0;
    playbackState.shouldResume = true;
    playbackState.recoveryAttempts = 0;
    clearPlaybackRecoveryTimer();
    title.textContent = playableTrack.title;
    applyPlaybackSettingsToMedia();
    showGlobalPlayer();
    syncVisiblePlaybackState();
    updateGlobalPlayerControls();

    try {
        await media.play();
        updateGlobalPlayerControls();
    } catch (error) {
        console.warn("Media playback failed", error);
        if (isAutoplayPolicyError(error)) {
            showToast(t("Press play in the media bar to start playback"), "warning");
        } else {
            schedulePlaybackRecovery();
        }
    }
}

function nextTrack(direction = 1) {
    const sequence = playbackState.queue.length ? playbackState.queue : visiblePlaybackTracks().map(toPlayableTrack);
    if (!sequence.length) return null;

    const current = playbackState.currentTrack || sequence[playbackState.currentIndex];

    if (playbackSettings.shuffle && direction > 0 && sequence.length > 1) {
        const choices = sequence.filter((track) => !sameTrack(track, current));
        return choices[Math.floor(Math.random() * choices.length)];
    }

    const currentInSequence = sequence.findIndex((track) => sameTrack(track, current));
    const startIndex = currentInSequence >= 0 ? currentInSequence : 0;
    const nextIndex = (startIndex + direction + sequence.length) % sequence.length;
    playbackState.currentIndex = nextIndex;
    return sequence[nextIndex];
}

async function handleTrackEnded() {
    const { media } = getPlayerElements();

    if (playbackSettings.repeat && media) {
        playbackState.shouldResume = true;
        media.currentTime = 0;
        await media.play();
        return;
    }

    if (playbackSettings.autoNext) {
        await playTrack(nextTrack(1), { resetQueue: false });
    } else {
        playbackState.shouldResume = false;
    }
}

let globalPlayerInitialized = false;

function initGlobalPlayer() {
    if (globalPlayerInitialized) return;
    globalPlayerInitialized = true;

    const { media, playPause, seek, volume, mute, shuffle, repeat } = getPlayerElements();
    if (media) {
        media.addEventListener("ended", handleTrackEnded);
        media.addEventListener("timeupdate", () => {
            if (Number.isFinite(media.currentTime) && media.currentTime > 0) {
                playbackState.lastKnownTime = media.currentTime;
            }
        });
        media.addEventListener("playing", () => {
            playbackState.shouldResume = true;
            playbackState.recoveryAttempts = 0;
            clearPlaybackRecoveryTimer();
        });
        media.addEventListener("error", () => {
            if (playbackState.currentTrack && playbackState.shouldResume) {
                console.warn("Media source failed", media.error);
                schedulePlaybackRecovery();
            }
        });
        ["timeupdate", "loadedmetadata", "durationchange", "play", "pause", "volumechange", "emptied"].forEach((eventName) => {
            media.addEventListener(eventName, updateGlobalPlayerControls);
        });
    }

    document.getElementById("globalPlayerPrev")?.addEventListener("click", () => playTrack(nextTrack(-1), { resetQueue: false }));
    document.getElementById("globalPlayerNext")?.addEventListener("click", () => playTrack(nextTrack(1), { resetQueue: false }));
    document.getElementById("globalPlayerClose")?.addEventListener("click", stopPlayback);

    playPause?.addEventListener("click", async () => {
        if (!media?.src) return;
        if (media.paused) {
            playbackState.shouldResume = true;
            try {
                if (media.error || media.readyState === HTMLMediaElement.HAVE_NOTHING) {
                    playbackState.recoveryAttempts = 0;
                    clearPlaybackRecoveryTimer();
                    await recoverCurrentPlayback();
                } else {
                    await media.play();
                }
            } catch (error) {
                console.warn("Media playback failed", error);
                if (isAutoplayPolicyError(error)) {
                    showToast(t("Playback was blocked. Press play again to resume."), "warning");
                } else {
                    schedulePlaybackRecovery();
                }
            }
        } else {
            playbackState.shouldResume = false;
            clearPlaybackRecoveryTimer();
            media.pause();
        }
        updateGlobalPlayerControls();
    });

    seek?.addEventListener("pointerdown", () => {
        playbackState.isSeeking = true;
    });
    seek?.addEventListener("input", () => {
        playbackState.isSeeking = true;
        setRangeProgress(seek, Number(seek.value), 1000);
        if (media && Number.isFinite(media.duration) && media.duration) {
            const currentTime = document.getElementById("globalPlayerCurrentTime");
            if (currentTime) currentTime.textContent = formatPlaybackTime((Number(seek.value) / 1000) * media.duration);
        }
    });
    // Seek once on release; seeking on every input event made the audio stutter while dragging.
    seek?.addEventListener("change", () => {
        if (media && Number.isFinite(media.duration) && media.duration) {
            media.currentTime = (Number(seek.value) / 1000) * media.duration;
        }
        playbackState.isSeeking = false;
        updateGlobalPlayerControls();
    });

    volume?.addEventListener("input", () => {
        if (!media) return;
        media.volume = Number(volume.value);
        media.muted = media.volume === 0;
        updateGlobalPlayerControls();
    });

    mute?.addEventListener("click", () => {
        if (!media) return;
        media.muted = !media.muted;
        updateGlobalPlayerControls();
    });

    shuffle?.addEventListener("click", () => {
        playbackSettings.shuffle = !playbackSettings.shuffle;
        savePlaybackSettings();
        updateGlobalPlayerControls();
    });

    repeat?.addEventListener("click", () => {
        playbackSettings.repeat = !playbackSettings.repeat;
        savePlaybackSettings();
        applyPlaybackSettingsToMedia();
    });

    document.getElementById("globalPlayerAutoNext")?.addEventListener("click", () => {
        playbackSettings.autoNext = !playbackSettings.autoNext;
        savePlaybackSettings();
        updateGlobalPlayerControls();
    });

    updateGlobalPlayerControls();
}

function initPlaylistTools() {
    const playlistSearch = document.getElementById("playlistSearch");
    const search = document.getElementById("videoSearch");
    const clearSearch = document.getElementById("clearVideoSearch");

    if (playlistSearch) playlistSearch.addEventListener("input", updatePlaylistSearch);
    updatePlaylistSearch();
    applyLibraryView(readLibraryView());
    document.querySelectorAll(".js-library-search").forEach((input) => {
        input.addEventListener("input", () => updateLibrarySearch(input));
    });

    if (search) search.addEventListener("input", updateVideoSearch);
    if (clearSearch) {
        clearSearch.addEventListener("click", () => {
            search.value = "";
            updateVideoSearch();
            search.focus();
        });
    }

    syncAutoSyncToggleLabels();
    refreshPlaybackTracks();
    updateVideoSearch();
}

function shouldHandleSoftNavigation(event, link) {
    if (!link || event.defaultPrevented || event.button !== 0) return false;
    if (!hasAuthenticatedShell() || isLoggingOut) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.hasAttribute("download") || link.dataset.noSoftNav !== undefined) return false;

    const rawHref = link.getAttribute("href");
    if (!rawHref || rawHref.startsWith("#")) return false;

    const url = new URL(rawHref, window.location.href);
    if (url.origin !== window.location.origin) return false;
    if (url.pathname.startsWith("/stream/")) return false;
    if (url.pathname === "/login") return false;

    return true;
}

function syncNavigationState() {
    const section = document.getElementById("shellHeader")?.dataset.navSection || "dashboard";
    document.querySelectorAll(".sidebar-link[data-nav]").forEach((link) => {
        link.classList.toggle("active", link.dataset.nav === section);
    });
}

async function navigateTo(url, options = {}) {
    const { push = true, scrollToTop = true, useCache = true } = options;
    const nextUrl = new URL(url, window.location.href);

    const cached = useCache ? cachedPage(nextUrl) : null;
    if (cached && renderPageSnapshot(cached, { push, scrollToTop })) {
        // Show the cached page at once, then quietly swap in fresh data.
        if (isPrefetchableUrl(nextUrl)) {
            fetchPageSnapshot(nextUrl, { force: true })
                .then((fresh) => {
                    if (fresh && navigationKey(window.location.href) === fresh.key) {
                        renderPageSnapshot(fresh, { push: false, scrollToTop: false });
                    }
                })
                .catch((error) => console.debug("Background refresh skipped", error));
        }
        return;
    }

    const snapshot = await fetchPageSnapshot(nextUrl, { navigateOnAuthRedirect: true });
    if (!snapshot || !renderPageSnapshot(snapshot, { push, scrollToTop })) {
        window.location.href = nextUrl.href;
    }
}

async function refreshCurrentPage() {
    try {
        await navigateTo(window.location.href, { push: false, scrollToTop: false, useCache: false });
    } catch (error) {
        console.warn("Soft refresh failed; falling back to full reload", error);
        window.location.reload();
    }
}

document.addEventListener("submit", async (event) => {
    if (event.target.matches('form[action="/logout"]')) {
        stopBackgroundNavigationWork();
        return;
    }

    if (event.target.matches(".js-history-page-jump")) {
        event.preventDefault();
        const form = event.target;
        const input = form.querySelector('input[name="page"]');
        const totalPages = Math.max(1, Number.parseInt(form.dataset.totalPages || "1", 10) || 1);
        const requestedPage = Number.parseInt(input?.value || "1", 10) || 1;
        const targetPage = Math.min(totalPages, Math.max(1, requestedPage));
        if (input) input.value = String(targetPage);

        const targetUrl = new URL(form.action, window.location.href);
        const params = new URLSearchParams(new FormData(form));
        targetUrl.search = params.toString();
        try {
            await navigateTo(targetUrl.href, { useCache: false });
        } catch (error) {
            console.warn("History page jump failed; falling back to full navigation", error);
            window.location.href = targetUrl.href;
        }
        return;
    }

    if (event.target.id === "addPlaylistForm") {
        event.preventDefault();
        const input = document.getElementById("playlistUrl");
        const button = event.target.querySelector('button[type="submit"]');
        const originalButtonHtml = button?.innerHTML;
        if (button) {
            button.disabled = true;
            button.innerHTML = `<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>${escapeHtml(t("Adding..."))}`;
        }
        try {
            const data = await requestJson("/playlist/add", {
                method: "POST",
                body: JSON.stringify({ url: input.value }),
            });
            invalidatePageCache();
            showToast(data.message || t("Playlist added"));
            await refreshActiveDownloads();
            await navigateTo(`/playlist/${data.playlist_id}`, { useCache: false });
            if (data.task_id) {
                pollTask(data.task_id).catch((error) => showToast(error.message, "danger"));
            }
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            if (button) {
                button.disabled = false;
                button.innerHTML = originalButtonHtml;
            }
        }
    }

    if (event.target.id === "createCustomPlaylistForm") {
        event.preventDefault();
        const form = event.target;
        const input = form.querySelector('[name="title"]');
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        try {
            const data = await requestJson("/playlist/custom", {
                method: "POST",
                body: JSON.stringify({ title: input?.value || "" }),
            });
            invalidatePageCache();
            showToast(data.message || t("Playlist created"));
            await navigateTo(`/playlist/${data.playlist_id}`, { useCache: false });
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            if (button) button.disabled = false;
        }
    }

    if (event.target.id === "addLibraryVideosForm") {
        event.preventDefault();
        const form = event.target;
        const videoIds = Array.from(form.querySelectorAll('input[name="video_ids"]:checked')).map((input) => Number(input.value));
        if (!videoIds.length) {
            showToast(t("Select at least one downloaded song"), "warning");
            return;
        }
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        try {
            const data = await requestJson(`/playlist/${form.dataset.playlistId}/videos/library`, {
                method: "POST",
                body: JSON.stringify({ video_ids: videoIds }),
            });
            invalidatePageCache();
            showToast(data.message || t("Songs added"));
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
            if (button) button.disabled = false;
        }
    }

    if (event.target.id === "addYoutubeVideosForm") {
        event.preventDefault();
        const form = event.target;
        const urls = (form.querySelector('[name="urls"]')?.value || "")
            .split(/\r?\n/)
            .map((url) => url.trim())
            .filter(Boolean);
        if (!urls.length || urls.length > 50) {
            showToast(t("Enter 1 to 50 YouTube links, one per line"), "warning");
            return;
        }
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        try {
            const data = await requestJson(`/playlist/${form.dataset.playlistId}/videos/youtube`, {
                method: "POST",
                body: JSON.stringify({ urls }),
            });
            form.reset();
            invalidatePageCache();
            showToast(data.message || t("MP3 download queued"));
            await refreshActiveDownloads();
            pollTask(data.task_id).catch((error) => showToast(error.message, "danger"));
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            if (button) button.disabled = false;
        }
    }

    if (event.target.id === "syncScheduleForm") {
        event.preventDefault();
        const form = event.target;
        const input = document.getElementById("syncTime");
        const button = form.querySelector('button[type="submit"]');
        const installed = form.dataset.installed === "true";
        if (button) button.disabled = true;
        try {
            const data = await requestJson(installed ? "/settings/sync-schedule" : "/settings/sync-schedule/install", {
                method: installed ? "PATCH" : "POST",
                body: JSON.stringify({ time: input.value }),
                timeoutMs: 45000,
            });
            invalidatePageCache();
            showToast(data.message || t("Settings saved"));
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            if (button) button.disabled = false;
        }
    }

    if (event.target.id === "changePasswordForm") {
        event.preventDefault();
        const form = event.target;
        const currentPassword = form.querySelector('[name="current_password"]')?.value || "";
        const newPassword = form.querySelector('[name="new_password"]')?.value || "";
        const confirmation = document.getElementById("confirmNewPassword")?.value || "";
        const button = form.querySelector('button[type="submit"]');

        if (newPassword !== confirmation) {
            showToast(t("The new password confirmation does not match"), "danger");
            return;
        }

        if (button) button.disabled = true;
        try {
            const data = await requestJson("/settings/password", {
                method: "PATCH",
                body: JSON.stringify({
                    current_password: currentPassword,
                    new_password: newPassword,
                }),
            });
            form.reset();
            showToast(data.message || t("Password changed successfully"));
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            if (button) button.disabled = false;
        }
    }
});

document.addEventListener("change", async (event) => {
    const autoSyncInput = event.target.closest?.(".js-auto-sync");
    if (!autoSyncInput) return;

    const nextValue = autoSyncInput.checked;
    autoSyncInput.disabled = true;
    updateAutoSyncToggle(autoSyncInput);
    try {
        const data = await requestJson(`/playlist/${autoSyncInput.dataset.playlistId}/auto-sync`, {
            method: "PATCH",
            body: JSON.stringify({ auto_sync: nextValue }),
        });
        invalidatePageCache();
        showToast(data.message || (nextValue ? t("Auto-sync enabled") : t("Auto-sync disabled")));
    } catch (error) {
        autoSyncInput.checked = !nextValue;
        updateAutoSyncToggle(autoSyncInput);
        showToast(error.message, "danger");
    } finally {
        autoSyncInput.disabled = false;
    }
});

const ORPHAN_PREVIEW_LIMIT = 20;
let orphanedMediaFiles = [];

function formatFileSize(bytes) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = Number(bytes || 0);
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
    }
    return unit === 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
}

function renderOrphanedMedia(result) {
    const panel = document.getElementById("orphanResult");
    const summary = document.getElementById("orphanSummary");
    const list = document.getElementById("orphanList");
    const deleteButton = document.getElementById("deleteOrphansButton");
    if (!panel || !summary || !list || !deleteButton) return;

    orphanedMediaFiles = result.files.map((file) => file.name);
    panel.classList.remove("d-none");
    list.replaceChildren();
    if (!result.count) {
        summary.textContent = t("No unused MP3 files found.");
        deleteButton.classList.add("d-none");
        return;
    }

    summary.textContent = t("{count} unused MP3 file(s), {size} in total.", { count: result.count, size: formatFileSize(result.total_bytes) });
    result.files.slice(0, ORPHAN_PREVIEW_LIMIT).forEach((file) => {
        const item = document.createElement("li");
        item.textContent = `${file.name} (${formatFileSize(file.size_bytes)})`;
        list.appendChild(item);
    });
    if (result.count > ORPHAN_PREVIEW_LIMIT) {
        const more = document.createElement("li");
        more.textContent = t("+{count} more file(s)", { count: result.count - ORPHAN_PREVIEW_LIMIT });
        list.appendChild(more);
    }
    deleteButton.querySelector("span").textContent = t("Delete {count} file(s)", { count: result.count });
    deleteButton.classList.remove("d-none");
}

async function findOrphanedMedia(button) {
    button.disabled = true;
    try {
        renderOrphanedMedia(await requestJson("/api/storage/orphans", { timeoutMs: 60000 }));
    } catch (error) {
        showToast(error.message, "danger");
    } finally {
        button.disabled = false;
    }
}

async function deleteOrphanedMedia(button) {
    if (!orphanedMediaFiles.length) return;
    const confirmed = window.confirm(
        t("Permanently delete {count} unused MP3 file(s)? This cannot be undone.", { count: orphanedMediaFiles.length }),
    );
    if (!confirmed) return;

    button.disabled = true;
    try {
        const data = await requestJson("/settings/storage/orphans/delete", {
            method: "POST",
            body: JSON.stringify({ files: orphanedMediaFiles }),
            timeoutMs: 120000,
        });
        orphanedMediaFiles = [];
        invalidatePageCache();
        showToast(data.message || t("Unused files deleted"));
        await refreshCurrentPage();
    } catch (error) {
        showToast(error.message, "danger");
        button.disabled = false;
    }
}

document.addEventListener("click", async (event) => {
    const viewButton = event.target.closest(".js-library-view");
    if (viewButton) {
        const view = viewButton.dataset.view === "list" ? "list" : "grid";
        try {
            window.localStorage.setItem(LIBRARY_VIEW_STORAGE_KEY, view);
        } catch {
            // The view still switches for this page when storage is unavailable.
        }
        applyLibraryView(view);
        storeCurrentPageSnapshot();
        return;
    }

    const passwordToggle = event.target.closest(".js-toggle-password");
    if (passwordToggle) {
        const input = document.getElementById(passwordToggle.getAttribute("aria-controls"));
        if (!input) return;
        const reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        passwordToggle.setAttribute("aria-pressed", String(reveal));
        passwordToggle.querySelector("i")?.classList.toggle("bi-eye", !reveal);
        passwordToggle.querySelector("i")?.classList.toggle("bi-eye-slash", reveal);
        return;
    }

    const ytDlpCheckButton = event.target.closest("#ytDlpCheckButton");
    if (ytDlpCheckButton) {
        ytDlpCheckButton.disabled = true;
        ytDlpCheckButton.classList.add("is-busy");
        try {
            const status = await requestJson("/api/yt-dlp/check", { method: "POST", timeoutMs: 120000 });
            renderYtDlpStatus(status);
            invalidatePageCache();
            showToast(status.last_error ? t("Update check failed") : t("yt-dlp is up to date"), status.last_error ? "danger" : "success");
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            ytDlpCheckButton.disabled = false;
            ytDlpCheckButton.classList.remove("is-busy");
        }
        return;
    }

    const openFolderButton = event.target.closest("#openStorageFolderButton");
    if (openFolderButton) {
        openFolderButton.disabled = true;
        try {
            await requestJson("/settings/storage/open-folder", { method: "POST" });
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            openFolderButton.disabled = false;
        }
        return;
    }

    const todaySyncToggle = event.target.closest(".js-today-sync-toggle");
    if (todaySyncToggle) {
        const panel = todaySyncToggle.closest(".today-sync-panel");
        const content = document.getElementById(todaySyncToggle.getAttribute("aria-controls"));
        if (!panel || !content) return;

        const expanded = todaySyncToggle.getAttribute("aria-expanded") !== "true";
        todaySyncToggle.setAttribute("aria-expanded", String(expanded));
        panel.classList.toggle("is-expanded", expanded);
        content.setAttribute("aria-hidden", String(!expanded));
        if (expanded) content.removeAttribute("inert");
        else content.setAttribute("inert", "");
        return;
    }

    if (event.target.closest("#activeDownloadsRefresh")) {
        await refreshActiveDownloads();
        return;
    }

    const uploadButton = event.target.closest("#uploadCustomMp3");
    if (uploadButton) {
        const input = document.getElementById("customMp3Files");
        const status = document.getElementById("customMp3UploadStatus");
        const files = Array.from(input?.files || []);
        if (!files.length) {
            showToast(t("Select at least one MP3 file"), "warning");
            return;
        }

        uploadButton.disabled = true;
        let completed = 0;
        const failures = [];
        for (const [index, file] of files.entries()) {
            if (status) status.textContent = t("Importing {current}/{total}: {name}", { current: index + 1, total: files.length, name: file.name });
            try {
                await uploadMp3File(uploadButton.dataset.playlistId, file);
                completed += 1;
            } catch (error) {
                failures.push(`${file.name}: ${error.message}`);
            }
        }

        invalidatePageCache();
        if (failures.length) {
            showToast(t("Imported {completed}/{total} file(s). {error}", { completed, total: files.length, error: failures[0] }), completed ? "warning" : "danger");
        } else {
            showToast(t("Imported {count} MP3 file(s)", { count: completed }));
        }
        if (status) status.textContent = failures.length ? t("Finished with {count} error(s)", { count: failures.length }) : t("Finished");
        await refreshCurrentPage();
        return;
    }

    const hideVideoButton = event.target.closest(".js-hide-video");
    if (hideVideoButton) {
        const title = hideVideoButton.dataset.title || t("this song");
        if (!window.confirm(t("Hide “{title}” from the playlist? Its data is kept.", { title }))) return;

        hideVideoButton.disabled = true;
        try {
            const data = await requestJson(
                `/playlist/${hideVideoButton.dataset.playlistId}/video/${hideVideoButton.dataset.videoId}`,
                { method: "DELETE" },
            );
            invalidatePageCache();
            showToast(data.message || t("Song hidden"));
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
            hideVideoButton.disabled = false;
        }
        return;
    }

    if (event.target.closest("#findOrphansButton")) {
        await findOrphanedMedia(event.target.closest("#findOrphansButton"));
        return;
    }

    if (event.target.closest("#deleteOrphansButton")) {
        await deleteOrphanedMedia(event.target.closest("#deleteOrphansButton"));
        return;
    }

    const restoreVideoButton = event.target.closest(".js-restore-video");
    if (restoreVideoButton) {
        restoreVideoButton.disabled = true;
        try {
            const data = await requestJson(
                `/playlist/${restoreVideoButton.dataset.playlistId}/video/${restoreVideoButton.dataset.videoId}/restore`,
                { method: "POST" },
            );
            invalidatePageCache();
            showToast(data.message || t("Song restored"));
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
            restoreVideoButton.disabled = false;
        }
        return;
    }

    const syncButton = event.target.closest(".js-sync");
    if (syncButton) {
        if (syncButton.disabled) return;
        syncButton.disabled = true;
        try {
            const data = await requestJson(`/playlist/${syncButton.dataset.playlistId}/sync`, {
                method: "POST",
                body: JSON.stringify({ format: syncButton.dataset.format || "mp3" }),
            });
            invalidatePageCache();
            showToast(t("Sync queued"));
            await refreshActiveDownloads();
            await pollTask(data.task_id);
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            syncButton.disabled = false;
        }
        return;
    }

    const playButton = event.target.closest(".js-play-media");
    if (playButton) {
        refreshPlaybackTracks();
        const track = playbackState.pageTracks.find((item) => item.button === playButton);
        await playTrack(track);
        return;
    }

    const deleteButton = event.target.closest(".js-delete");
    if (deleteButton) {
        if (!window.confirm(t("Delete this playlist from the archive? Its MP3 files are kept."))) return;
        try {
            const data = await requestJson(`/playlist/${deleteButton.dataset.playlistId}`, { method: "DELETE" });
            invalidatePageCache();
            showToast(data.message || t("Playlist deleted"));
            window.setTimeout(() => refreshCurrentPage(), 700);
        } catch (error) {
            showToast(error.message, "danger");
        }
    }


    const link = event.target.closest("a");
    if (shouldHandleSoftNavigation(event, link)) {
        event.preventDefault();
        try {
            await navigateTo(link.href);
        } catch (error) {
            console.warn("Soft navigation failed; falling back to full navigation", error);
            window.location.href = link.href;
        }
    }
});

["pointerover", "focusin", "touchstart", "mousedown"].forEach((eventName) => {
    document.addEventListener(eventName, (event) => {
        if (!hasAuthenticatedShell() || isLoggingOut) return;
        const link = linkFromEvent(event);
        if (!link || !isPrefetchableUrl(link.href)) return;
        prefetchNavigationUrl(link.href);
    }, { passive: true });
});

window.addEventListener("popstate", () => {
    navigateTo(window.location.href, { push: false }).catch((error) => {
        console.warn("History navigation failed; falling back to full navigation", error);
        window.location.reload();
    });
});

document.addEventListener("DOMContentLoaded", () => {
    window.history.replaceState({ softNavigation: true }, "", window.location.href);
    syncNavigationState();
    initGlobalPlayer();
    initPlaylistTools();
    storeCurrentPageSnapshot();
    scheduleActiveDownloadsRefresh(1000);
    scheduleActiveDownloadsPoll(2500);
    refreshYtDlpStatus();
    window.setInterval(refreshYtDlpStatus, 5 * 60 * 1000);
});
