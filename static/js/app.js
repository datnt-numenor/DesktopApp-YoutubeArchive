function showToast(message, tone = "success") {
    const area = document.getElementById("toastArea");
    if (!area) return;

    const toast = document.createElement("div");
    toast.className = `toast align-items-center text-bg-${tone} border-0`;
    toast.setAttribute("role", "alert");
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body"></div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
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
        throw new Error("Authentication required");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || data.message || `Request failed with ${response.status}`);
    }
    return data;
}

const YTDLP_VERSION_STORAGE_KEY = "ytarchiveYtDlpVersion";
let ytDlpStatusRefreshing = false;

function formatLocalTimestamp(value) {
    if (!value) return null;
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) return value;
    return timestamp.toLocaleString("vi-VN", {
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

    version.textContent = status.installed_version || "Not installed";
    previous.textContent = status.previous_version
        ? `Previous: ${status.previous_version}`
        : "No previous version recorded";
    result.textContent = status.last_error ? "Update check failed" : "Ready";
    lastCheck.textContent = status.last_successful_check_at
        ? `Last successful check: ${formatLocalTimestamp(status.last_successful_check_at)}`
        : "Not checked yet";
    lastUpdated.textContent = status.last_updated_at
        ? `Last updated: ${formatLocalTimestamp(status.last_updated_at)}`
        : "No completed update recorded yet.";
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
            showToast(`yt-dlp updated: ${previousSeenVersion} → ${currentVersion}`);
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
    if (!response.ok) throw new Error(data.detail || `Upload failed with ${response.status}`);
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
    currentContent.innerHTML = snapshot.contentHtml;
    if (push) {
        window.history.pushState({ softNavigation: true }, "", snapshot.finalUrl);
    }
    collapseTopNav();
    syncNavigationState();
    initPlaylistTools();
    storeCurrentPageSnapshot();
    scheduleNavigationPrefetch();
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
    if (navigationKey(nextUrl) === navigationKey(window.location.href)) return Promise.resolve(null);
    return fetchPageSnapshot(nextUrl, options).catch((error) => {
        console.debug("Navigation prefetch skipped", error);
        return null;
    });
}

function scheduleNavigationPrefetch() {
    if (!hasAuthenticatedShell() || isLoggingOut) return;
    const run = () => {
        if (!hasAuthenticatedShell() || isLoggingOut) return;
        const seen = new Set();
        const links = Array.from(document.querySelectorAll(".app-sidebar a[href], .app-topbar a[href], .public-nav a[href], #pageContent a[href]"));
        const candidates = links
            .map((link) => new URL(link.href, window.location.href))
            .filter((url) => {
                const key = navigationKey(url);
                if (seen.has(key) || !isCacheableNavigationUrl(url) || key === navigationKey(window.location.href)) {
                    return false;
                }
                seen.add(key);
                return true;
            })
            .slice(0, PAGE_PREFETCH_LIMIT);

        candidates.forEach((url, index) => {
            window.setTimeout(() => prefetchNavigationUrl(url), 120 * index);
        });
    };

    window.setTimeout(run, PAGE_PREFETCH_START_DELAY_MS);
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
    if (!total) return "Preparing";
    return `${processed}/${total} processed | ${completed} downloaded | ${failed} failed`;
}

function taskStatusLabel(task) {
    if (task.status === "done" && Number(task.failed || 0) > 0) return "done with errors";
    return task.status;
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
        const playlistTitle = task.playlist_title || `Playlist ${task.playlist_id || ""}`;
        const current = task.current_video ? ` | ${escapeHtml(task.current_video)}` : "";
        const tone = task.status === "failed" || Number(task.failed || 0) > 0 ? "bg-warning" : "bg-danger";
        const errors = Array.isArray(task.errors) ? task.errors : [];
        const visibleErrors = errors.slice(0, 5);
        const hiddenErrorCount = Math.max(0, errors.length - visibleErrors.length);
        const errorHtml = visibleErrors.length ? `
            <div class="active-download-errors">
                <div class="active-download-errors-title">
                    <i class="bi bi-exclamation-triangle me-1"></i>Failed videos
                </div>
                ${visibleErrors.map((item) => `
                    <div class="active-download-error-item">
                        <strong>${escapeHtml(item.title || "Unknown video")}</strong>
                        <span class="text-muted">(${escapeHtml(item.yt_video_id || "")})</span>:
                        ${escapeHtml(item.message || "Download failed")}
                    </div>
                `).join("")}
                ${hiddenErrorCount ? `<div class="active-download-error-item text-muted">+${hiddenErrorCount} more failed video(s)</div>` : ""}
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
                    <a class="btn btn-sm btn-outline-secondary" href="/playlist/${task.playlist_id}" title="Open playlist">
                        <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                </div>
                <div class="progress active-download-progress" role="progressbar" aria-label="Active download progress">
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

async function pollTask(taskId) {
    let done = false;
    while (!done) {
        const task = await requestJson(`/task/${taskId}/status`, { timeoutMs: 3000 });
        if (task.status === "done") {
            invalidatePageCache();
            if (Number(task.failed || 0) > 0) {
                showToast(`Sync done: ${task.completed} downloaded, ${task.failed} failed`, "warning");
                window.setTimeout(() => refreshCurrentPage(), 1200);
            } else {
                showToast(task.format === "metadata" ? "Metadata imported" : "Sync completed");
                window.setTimeout(() => refreshCurrentPage(), 700);
            }
            done = true;
            refreshYtDlpStatus();
        } else if (task.status === "failed") {
            showToast(task.error || "Sync failed", "danger");
            done = true;
        } else {
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
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

function updatePlaylistSearch() {
    const input = document.getElementById("playlistSearch");
    const cards = Array.from(document.querySelectorAll(".js-playlist-card"));
    const count = document.getElementById("playlistSearchCount");
    const empty = document.getElementById("playlistSearchEmpty");
    if (!input && !cards.length) return;

    const query = normalizeSearch(input?.value || "");
    let visibleCount = 0;

    cards.forEach((card) => {
        const playlistText = [
            card.dataset.title,
            card.dataset.url,
        ].join(" ");
        const songMatches = Array.from(card.querySelectorAll(".dashboard-song-index span")).filter((song) => {
            const songText = [song.dataset.title, song.dataset.channel, song.dataset.sourceId].join(" ");
            return query && normalizeSearch(songText).includes(query);
        });
        const playlistMatches = !query || normalizeSearch(playlistText).includes(query);
        const matches = playlistMatches || songMatches.length > 0;
        card.classList.toggle("d-none", !matches);
        const matchBox = card.querySelector(".dashboard-song-matches");
        if (matchBox) {
            if (query && songMatches.length) {
                const names = songMatches.slice(0, 3).map((song) => song.textContent.trim());
                const remaining = songMatches.length - names.length;
                matchBox.textContent = `Khớp bài: ${names.join(", ")}${remaining ? ` (+${remaining})` : ""}`;
                matchBox.classList.remove("d-none");
            } else {
                matchBox.textContent = "";
                matchBox.classList.add("d-none");
            }
        }
        if (matches) visibleCount += 1;
    });

    if (count) {
        count.textContent = query ? `${visibleCount}/${cards.length} shown` : `${cards.length} total`;
    }
    if (empty) empty.classList.toggle("d-none", !query || visibleCount > 0);
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
    if (label) label.textContent = input.checked ? "Auto-sync on" : "Auto-sync off";
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

    if (count) count.textContent = `${visibleCount}/${rows.length} videos shown`;
    if (empty) empty.classList.toggle("d-none", visibleCount > 0);
    syncVisiblePlaybackState();
}

function refreshPlaybackTracks() {
    playbackState.pageTracks = Array.from(document.querySelectorAll(".js-play-media")).map((button) => ({
        id: button.dataset.videoId || button.dataset.streamUrl,
        button,
        row: button.closest(".js-video-row"),
        title: button.dataset.title || "Untitled track",
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
        title: track.title || "Untitled track",
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
    const { media, playPause, seek, currentTime, duration, volume, mute, shuffle, repeat } = getPlayerElements();
    if (!media) return;

    if (playPause) {
        playPause.innerHTML = media.paused ? '<i class="bi bi-play-fill"></i>' : '<i class="bi bi-pause-fill"></i>';
        playPause.classList.toggle("is-active", !media.paused);
    }

    const mediaDuration = Number.isFinite(media.duration) ? media.duration : 0;
    if (seek) {
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
        showToast("Local media server is unavailable. Please restart the app if playback does not recover.", "danger");
        return;
    }

    if (playbackState.recoveryAttempts === 0) {
        showToast("Local media connection was interrupted. Reconnecting...", "warning");
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
            showToast("Press play in the media bar to resume playback", "warning");
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
    if (title) title.textContent = "No track selected";
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
        item.row?.classList.toggle("table-active", active);
        item.button.classList.toggle("btn-danger", active);
        item.button.classList.toggle("btn-outline-secondary", !active);
        item.button.innerHTML = active ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-play-circle"></i>';
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
            showToast("Press play in the media bar to start playback", "warning");
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

function bindPlaybackOptions() {
    const options = [
        ["playbackRepeat", "repeat"],
        ["playbackAutoNext", "autoNext"],
        ["playbackShuffle", "shuffle"],
    ];
    options.forEach(([elementId, settingKey]) => {
        const input = document.getElementById(elementId);
        if (!input) return;
        input.checked = Boolean(playbackSettings[settingKey]);
        input.addEventListener("change", () => {
            playbackSettings[settingKey] = input.checked;
            savePlaybackSettings();
            applyPlaybackSettingsToMedia();
            updateGlobalPlayerControls();
        });
    });
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
                    showToast("Playback was blocked. Press play again to resume.", "warning");
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

    seek?.addEventListener("input", () => {
        if (!media || !Number.isFinite(media.duration) || !media.duration) return;
        media.currentTime = (Number(seek.value) / 1000) * media.duration;
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

    updateGlobalPlayerControls();
}

function initPlaylistTools() {
    const playlistSearch = document.getElementById("playlistSearch");
    const search = document.getElementById("videoSearch");
    const clearSearch = document.getElementById("clearVideoSearch");

    if (playlistSearch) playlistSearch.addEventListener("input", updatePlaylistSearch);
    updatePlaylistSearch();
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

    bindPlaybackOptions();
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

function collapseTopNav() {
    const nav = document.getElementById("topNav");
    if (!nav?.classList.contains("show") || !window.bootstrap) return;
    bootstrap.Collapse.getOrCreateInstance(nav, { toggle: false }).hide();
}

function sectionForPath(pathname) {
    if (pathname.startsWith("/history")) {
        return { nav: "history", label: "History" };
    }
    if (pathname.startsWith("/settings")) {
        return { nav: "settings", label: "Settings" };
    }
    if (pathname.startsWith("/playlist/")) {
        return { nav: "dashboard", label: "Playlist detail" };
    }
    return { nav: "dashboard", label: "Dashboard" };
}

function syncNavigationState() {
    const section = sectionForPath(window.location.pathname);
    document.querySelectorAll(".sidebar-link[data-nav]").forEach((link) => {
        link.classList.toggle("active", link.dataset.nav === section.nav);
    });
    const label = document.getElementById("currentSectionLabel");
    if (label) label.textContent = section.label;
}

async function navigateTo(url, options = {}) {
    const { push = true, scrollToTop = true, useCache = true } = options;
    const nextUrl = new URL(url, window.location.href);

    const cached = useCache ? cachedPage(nextUrl) : null;
    if (cached && renderPageSnapshot(cached, { push, scrollToTop })) {
        prefetchNavigationUrl(nextUrl, { force: true });
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
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Adding...';
        }
        try {
            const data = await requestJson("/playlist/add", {
                method: "POST",
                body: JSON.stringify({ url: input.value }),
            });
            invalidatePageCache();
            showToast(data.message || "Playlist added");
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
            showToast(data.message || "Đã tạo playlist");
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
            showToast("Hãy chọn ít nhất một bài đã tải", "warning");
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
            showToast(data.message || "Đã thêm bài hát");
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
            showToast("Nhập từ 1 đến 50 link YouTube, mỗi dòng một link", "warning");
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
            showToast(data.message || "Đã xếp hàng tải MP3");
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
        const input = document.getElementById("syncTime");
        try {
            const data = await requestJson("/settings/sync-schedule", {
                method: "PATCH",
                body: JSON.stringify({ time: input.value }),
            });
            invalidatePageCache();
            showToast(data.message || "Settings saved");
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
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
            showToast("New password confirmation does not match", "danger");
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
            showToast(data.message || "Password changed successfully");
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
        showToast(data.message || (nextValue ? "Auto-sync enabled" : "Auto-sync disabled"));
    } catch (error) {
        autoSyncInput.checked = !nextValue;
        updateAutoSyncToggle(autoSyncInput);
        showToast(error.message, "danger");
    } finally {
        autoSyncInput.disabled = false;
    }
});

document.addEventListener("click", async (event) => {
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
            showToast("Hãy chọn ít nhất một file MP3", "warning");
            return;
        }

        uploadButton.disabled = true;
        let completed = 0;
        const failures = [];
        for (const [index, file] of files.entries()) {
            if (status) status.textContent = `Đang nhập ${index + 1}/${files.length}: ${file.name}`;
            try {
                await uploadMp3File(uploadButton.dataset.playlistId, file);
                completed += 1;
            } catch (error) {
                failures.push(`${file.name}: ${error.message}`);
            }
        }

        invalidatePageCache();
        if (failures.length) {
            showToast(`Đã nhập ${completed}/${files.length} file. ${failures[0]}`, completed ? "warning" : "danger");
        } else {
            showToast(`Đã nhập ${completed} file MP3`);
        }
        if (status) status.textContent = failures.length ? `Hoàn tất với ${failures.length} lỗi` : "Hoàn tất";
        await refreshCurrentPage();
        return;
    }

    const hideVideoButton = event.target.closest(".js-hide-video");
    if (hideVideoButton) {
        const title = hideVideoButton.dataset.title || "bài hát này";
        if (!window.confirm(`Ẩn “${title}” khỏi playlist? Dữ liệu vẫn được giữ lại.`)) return;

        hideVideoButton.disabled = true;
        try {
            const data = await requestJson(
                `/playlist/${hideVideoButton.dataset.playlistId}/video/${hideVideoButton.dataset.videoId}`,
                { method: "DELETE" },
            );
            invalidatePageCache();
            showToast(data.message || "Đã ẩn bài hát");
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
            hideVideoButton.disabled = false;
        }
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
            showToast(data.message || "Đã khôi phục bài hát");
            await refreshCurrentPage();
        } catch (error) {
            showToast(error.message, "danger");
            restoreVideoButton.disabled = false;
        }
        return;
    }

    const syncButton = event.target.closest(".js-sync");
    if (syncButton) {
        syncButton.disabled = true;
        try {
            const data = await requestJson(`/playlist/${syncButton.dataset.playlistId}/sync`, {
                method: "POST",
                body: JSON.stringify({ format: syncButton.dataset.format || "mp3" }),
            });
            invalidatePageCache();
            showToast("Sync queued");
            await refreshActiveDownloads();
            pollTask(data.task_id).catch((error) => showToast(error.message, "danger"));
        } catch (error) {
            showToast(error.message, "danger");
        } finally {
            syncButton.disabled = false;
        }
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
        if (!window.confirm("Delete this playlist from the archive?")) return;
        try {
            const data = await requestJson(`/playlist/${deleteButton.dataset.playlistId}`, { method: "DELETE" });
            invalidatePageCache();
            showToast(data.message || "Playlist deleted");
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
        if (!link || !isCacheableNavigationUrl(link.href)) return;
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
    scheduleNavigationPrefetch();
    scheduleActiveDownloadsRefresh(1000);
    scheduleActiveDownloadsPoll(2500);
    refreshYtDlpStatus();
    window.setInterval(refreshYtDlpStatus, 5 * 60 * 1000);
});
