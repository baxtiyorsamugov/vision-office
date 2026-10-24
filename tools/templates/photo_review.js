"use strict";
(() => {
  const report = JSON.parse(document.getElementById("report").textContent);
  const unknownReview = report.review_scope === "unknown";
  const $ = (id) => document.getElementById(id);
  const events = new Map(report.events.map((row) => [row.id, row]));
  const people = new Map(report.people.map((row) => [row.id, row]));
  const labels = {
    unreviewed: "Не проверено",
    confirmed: "Подтверждено",
    unsure: "Спорно",
    rejected: "Отклонено",
  };
  const reasons = new Set([
    "quality",
    "occluded",
    "no_face",
    "duplicate",
    "other",
    "journal_not_confirmed",
  ]);
  const key = "vision-office-photo-review-v1:" + report.snapshot_id;
  const backupKey = key + ":before-name-candidates-v1";
  let lastStoredRaw = null,
    backupSession = null,
    backupBlocked = false;
  const pageSize = 24;
  let decisions = new Map(),
    history = [],
    filtered = [],
    selected = null,
    personId = null,
    nameCandidateId = null;
  let status = "unreviewed",
    page = 0,
    peopleLimit = 40,
    frameReady = false;
  let referenceReady = false,
    referenceGeneration = 0;
  let dirtyExport = false,
    storageBroken = false,
    pendingImport = null;
  const dates = new Map();
  const dateFormat = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Tashkent",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const timeFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Asia/Tashkent",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const category = (person) =>
    person.person_type === "student" ? "Студент" : "Сотрудник";
  for (const event of report.events)
    dates.set(event.id, dateFormat.format(new Date(event.at)));
  function error(message) {
    $("error").textContent = message;
    $("error").hidden = !message;
  }
  function state(id) {
    return decisions.get(id)?.status || "unreviewed";
  }
  function journalMode() {
    return !unknownReview && $("reviewMode").value === "journal";
  }
  function journalPerson(row = events.get(selected)) {
    if (!row || row.prediction_type === "unknown") return null;
    const person = people.get(row.prediction_person_id);
    const expected = {
      eduschool_student: "student",
      eduschool_employee: "employee",
    }[row.prediction_type];
    return person && expected === person.person_type ? person : null;
  }
  function nameTokens(value) {
    return (
      String(value || "")
        .normalize("NFKD")
        .toLocaleLowerCase("ru-RU")
        .replace(/\p{M}/gu, "")
        .replace(/[’‘ʻʼ`´'ʹ]/g, "")
        .match(/[\p{L}\p{N}]+/gu) || []
    );
  }
  const nameIndex = report.people.map((person) => ({
    person,
    tokens: [...new Set(nameTokens(person.full_name))],
  }));
  const nameCache = new Map();
  function nameCandidates(row = events.get(selected)) {
    if (!row || journalPerson(row)) return [];
    if (nameCache.has(row.id)) return nameCache.get(row.id);
    const kind = {
      employee: "employee",
      eduschool_employee: "employee",
      eduschool_student: "student",
    }[row.prediction_type];
    const tokens = [...new Set(nameTokens(row.prediction_name))];
    const candidates = [];
    if (kind && tokens.length >= 2) {
      for (const entry of nameIndex) {
        if (!entry.person.active || entry.person.person_type !== kind) continue;
        const shared = tokens.filter(
          (token) => token.length > 1 && entry.tokens.includes(token),
        );
        if (shared.length < 2) continue;
        const exact =
          tokens.length === entry.tokens.length &&
          shared.length === tokens.length;
        candidates.push({ person: entry.person, shared: shared.length, exact });
      }
    }
    candidates.sort(
      (a, b) =>
        Number(b.exact) - Number(a.exact) ||
        b.shared - a.shared ||
        a.person.full_name.localeCompare(b.person.full_name, "ru") ||
        a.person.id.localeCompare(b.person.id),
    );
    nameCache.set(row.id, candidates);
    return candidates;
  }
  function selectedNameCandidate() {
    return (
      nameCandidates().find(
        (candidate) => candidate.person.id === nameCandidateId,
      )?.person ?? null
    );
  }
  function displayedPerson() {
    return journalMode()
      ? journalPerson() || selectedNameCandidate()
      : people.get(personId);
  }
  function icon(name) {
    const image = document.createElement("img");
    image.src = "icons/" + name + ".svg";
    image.alt = "";
    return image;
  }
  function session() {
    return {
      version: 1,
      purpose: "manual_photo_review",
      snapshot_id: report.snapshot_id,
      dump_sha256: report.dump_sha256,
      exported_at: new Date().toISOString(),
      applied: false,
      decisions: Object.fromEntries(decisions),
    };
  }
  function validateRecord(id, value) {
    const event = events.get(id);
    if (
      !event ||
      !value ||
      Array.isArray(value) ||
      typeof value !== "object" ||
      !["confirmed", "unsure", "rejected"].includes(value.status) ||
      value.image_sha256 !== event.sha256 ||
      typeof value.decided_at !== "string" ||
      !Number.isFinite(Date.parse(value.decided_at))
    )
      throw new Error("Некорректная запись решения.");
    if (value.status === "confirmed") {
      if (
        !people.get(value.person_id)?.active ||
        value.human_confirmed !== true ||
        value.reason !== null
      )
        throw new Error("Подтверждение человека недействительно.");
    } else if (
      value.person_id !== null ||
      value.human_confirmed !== false ||
      (value.status === "rejected"
        ? !reasons.has(value.reason)
        : value.reason !== null)
    )
      throw new Error("Некорректный статус кадра.");
    const method = value.review_method ?? "manual";
    const journalId = value.journal_person_id ?? null;
    const referenceApproved =
      value.reference_approved ??
      (method === "manual" && value.status === "confirmed");
    if (
      !["manual", "journal_label", "name_candidate"].includes(method) ||
      typeof referenceApproved !== "boolean" ||
      (value.status !== "confirmed" && referenceApproved)
    )
      throw new Error("Некорректный способ проверки.");
    if (method === "journal_label") {
      const person = journalPerson(event);
      if (
        event.prediction_type === "unknown" ||
        journalId !== (person?.id ?? null) ||
        (value.status === "confirmed" &&
          (!person?.references.length || value.person_id !== person.id))
      )
        throw new Error("Решение не соответствует ID из журнала.");
    } else if (method === "name_candidate") {
      const candidate = nameCandidates(event).find(
        (item) => item.person.id === value.name_candidate_id,
      )?.person;
      if (
        !candidate ||
        journalId !== null ||
        (value.status === "confirmed" &&
          (value.person_id !== candidate.id || !candidate.references.length))
      )
        throw new Error("Кандидат не соответствует тексту ФИО из журнала.");
    } else if (
      journalId !== null ||
      (value.status === "confirmed" && !referenceApproved)
    ) {
      throw new Error("Некорректное ручное подтверждение.");
    }
    return {
      status: value.status,
      person_id: value.person_id,
      image_sha256: event.sha256,
      human_confirmed: value.human_confirmed,
      reason: value.reason,
      decided_at: value.decided_at,
      review_method: method,
      journal_person_id: journalId,
      reference_approved: referenceApproved,
      ...(method === "name_candidate"
        ? { name_candidate_id: value.name_candidate_id }
        : {}),
    };
  }
  function validateSession(value) {
    if (
      !value ||
      value.version !== 1 ||
      value.purpose !== "manual_photo_review" ||
      value.applied !== false ||
      value.snapshot_id !== report.snapshot_id ||
      value.dump_sha256 !== report.dump_sha256 ||
      !value.decisions ||
      typeof value.decisions !== "object" ||
      Array.isArray(value.decisions)
    )
      throw new Error("Файл не относится к этой копии данных.");
    if (Object.keys(value.decisions).length > events.size)
      throw new Error("Слишком много решений.");
    return new Map(
      Object.entries(value.decisions).map(([id, record]) => [
        id,
        validateRecord(id, record),
      ]),
    );
  }
  function save() {
    dirtyExport = true;
    try {
      if (backupBlocked)
        throw new Error(
          "Не удалось создать резервную копию. Прежняя разметка не перезаписана; экспортируй новые решения в JSON.",
        );
      if (localStorage.getItem(key) !== lastStoredRaw)
        throw new Error(
          "Разметка изменилась в другой вкладке. Она не перезаписана; экспортируй текущие решения в JSON и объедини их через импорт.",
        );
      const payload = JSON.stringify({
        session: session(),
        history: history.slice(-100),
      });
      localStorage.setItem(key, payload);
      lastStoredRaw = payload;
      $("saveState").textContent =
        "Прогресс сохранён в браузере · JSON не экспортирован";
    } catch (problem) {
      $("saveState").textContent = "Только в памяти · требуется экспорт JSON";
      error(
        problem.name === "Error"
          ? problem.message
          : "Браузер не сохранил прогресс. Экспортируй разметку перед закрытием страницы.",
      );
    }
  }
  try {
    const stored = localStorage.getItem(key);
    lastStoredRaw = stored;
    if (stored) {
      const parsed = JSON.parse(stored);
      const validated = validateSession(parsed.session);
      if (!Array.isArray(parsed.history) || parsed.history.length > 100)
        throw new Error("Повреждена история решений.");
      const oldHistory = parsed.history.map((item) => {
        if (!events.has(item.id))
          throw new Error("Неизвестный кадр в истории.");
        return {
          id: item.id,
          previous:
            item.previous === null
              ? null
              : validateRecord(item.id, item.previous),
        };
      });
      decisions = validated;
      history = oldHistory;
      $("saveState").textContent = "Прогресс восстановлен из браузера";
      try {
        const oldBackup = localStorage.getItem(backupKey);
        if (oldBackup === null) localStorage.setItem(backupKey, stored);
        backupSession = JSON.parse(oldBackup ?? stored).session;
        validateSession(backupSession);
        $("backupButton").disabled = false;
      } catch (_) {
        backupBlocked = true;
        error(
          "Прогресс восстановлен, но резервная копия недоступна. Прежняя разметка останется нетронутой; новые решения нужно экспортировать в JSON.",
        );
      }
    } else $("saveState").textContent = "Решений пока нет";
  } catch (problem) {
    storageBroken = problem.name !== "SecurityError";
    if (storageBroken)
      error(
        "Сохранённый прогресс повреждён. Решения заблокированы до импорта корректного JSON; старое содержимое не перезаписано.",
      );
    else {
      $("saveState").textContent = "Только в памяти · требуется экспорт JSON";
      error(
        "Браузер запретил локальное сохранение. Экспортируй разметку перед закрытием страницы.",
      );
    }
  }
  function options(id, values) {
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      $(id).append(option);
    }
  }
  options(
    "camera",
    [...new Set(report.events.map((row) => row.camera))].sort(),
  );
  options("date", [...new Set(dates.values())].sort().reverse());
  $("datasetStats").textContent =
    `${report.events.length.toLocaleString("ru-RU")} кадров · ${report.people.filter((p) => p.active).length.toLocaleString("ru-RU")} активных людей · Asia/Tashkent`;
  $("missingInfo").textContent =
    `В копии отсутствуют ${(report.skipped.events || 0).toLocaleString("ru-RU")} фото событий; они не включены в очередь.`;
  const namedEvents = report.events.filter(
    (row) => row.prediction_type !== "unknown",
  );
  $("journalCount").textContent =
    `${namedEvents.length} кадров с именем · ${namedEvents.filter((row) => journalPerson(row)).length} связаны с карточкой по ID`;
  function stats() {
    const scope = journalMode() ? namedEvents : report.events;
    const counts = {
      all: scope.length,
      unreviewed: 0,
      confirmed: 0,
      unsure: 0,
      rejected: 0,
    };
    const confirmed = new Set();
    let totalConfirmed = 0;
    for (const row of scope) counts[state(row.id)]++;
    for (const value of decisions.values()) {
      if (value.status === "confirmed") {
        confirmed.add(value.person_id);
        totalConfirmed++;
      }
    }
    for (const button of $("tabs").children) {
      button.querySelector("b").textContent =
        counts[button.dataset.status].toLocaleString("ru-RU");
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.status === status),
      );
    }
    const approved = [...decisions.values()].filter(
      (value) => value.status === "confirmed" && value.reference_approved,
    ).length;
    $("confirmedPeople").textContent =
      `Всего подтверждено: ${totalConfirmed} кадров для ${confirmed.size} людей · Отобрано для эталонов: ${approved}`;
    $("undo").disabled = !history.length || storageBroken;
  }
  function rebuild(preferred = selected) {
    const camera = $("camera").value,
      date = $("date").value,
      source = $("source").value,
      size = $("size").value;
    filtered = report.events.filter((row) => {
      const small = Math.min(row.width, row.height),
        unknown = row.prediction_type === "unknown";
      return (
        (!journalMode() || !unknown) &&
        (status === "all" || state(row.id) === status) &&
        (!camera || row.camera === camera) &&
        (!date || dates.get(row.id) === date) &&
        (!source || (source === "unknown" ? unknown : !unknown)) &&
        (!size || (size === "small" ? small < 80 : small >= Number(size)))
      );
    });
    const sort = $("sort").value;
    filtered.sort((a, b) =>
      sort === "similar"
        ? (b.suggested_similarity ?? -1) - (a.suggested_similarity ?? -1) ||
          a.id.localeCompare(b.id)
        : sort === "largest"
        ? Math.min(b.width, b.height) - Math.min(a.width, a.height) ||
          a.id.localeCompare(b.id)
        : (sort === "oldest" ? 1 : -1) *
            (Date.parse(a.at) - Date.parse(b.at)) || a.id.localeCompare(b.id),
    );
    const index = filtered.findIndex((row) => row.id === preferred);
    page = index >= 0 ? Math.floor(index / pageSize) : 0;
    selected = index >= 0 ? preferred : filtered[0]?.id || null;
    stats();
    renderQueue();
    inspect();
  }
  function renderQueue() {
    $("queue").replaceChildren();
    for (const row of filtered.slice(page * pageSize, (page + 1) * pageSize)) {
      const button = document.createElement("button");
      button.className = "photo-tile";
      button.dataset.event = row.id;
      button.dataset.state = state(row.id);
      button.setAttribute("aria-selected", String(row.id === selected));
      button.title = `${dates.get(row.id)} ${timeFormat.format(new Date(row.at))} · ${row.camera} · ${labels[state(row.id)]}`;
      button.setAttribute("aria-label", button.title);
      const image = document.createElement("img");
      image.src = row.image;
      image.alt = "Кадр камеры";
      image.loading = "lazy";
      const caption = document.createElement("span");
      caption.textContent = timeFormat.format(new Date(row.at));
      const dimensions = document.createElement("span");
      dimensions.textContent = `${row.width} × ${row.height} px`;
      button.append(image, caption, dimensions);
      button.onclick = () => {
        selected = row.id;
        renderQueue();
        inspect();
      };
      $("queue").append(button);
    }
    $("empty").hidden = !!filtered.length;
    $("queue").hidden = !filtered.length;
    $("resultCount").textContent = filtered.length.toLocaleString("ru-RU");
    $("pageInfo").textContent = filtered.length
      ? `${page + 1} / ${Math.ceil(filtered.length / pageSize)}`
      : "0 / 0";
    $("prevPage").disabled = page === 0;
    $("nextPage").disabled = (page + 1) * pageSize >= filtered.length;
  }
  function clearImage(image) {
    image.onload = null;
    image.onerror = null;
    image.removeAttribute("src");
  }
  function inspect() {
    const row = events.get(selected);
    $("inspection").hidden = !row;
    $("noSelection").hidden = !!row;
    $("verified").checked = false;
    if (unknownReview) $("rejectReason").value = "";
    frameReady = false;
    referenceReady = false;
    referenceGeneration++;
    personId = null;
    nameCandidateId = null;
    clearImage($("frameImage"));
    clearImage($("referenceImage"));
    const index = filtered.findIndex((event) => event.id === selected);
    $("prevFrame").disabled = index <= 0;
    $("nextFrame").disabled = index < 0 || index >= filtered.length - 1;
    if (!row) {
      controls();
      return;
    }
    const current = decisions.get(selected);
    if (!journalMode() && current?.status === "confirmed")
      personId = current.person_id;
    // A similarity hint only preselects the card; the operator still compares and confirms.
    else if (unknownReview && !current && people.get(row.suggested_person_id)?.active)
      personId = row.suggested_person_id;
    if (journalMode() && current?.review_method === "name_candidate")
      nameCandidateId = current.name_candidate_id;
    $("frameImage").onload = () => {
      if (selected === row.id) {
        frameReady = true;
        controls();
      }
    };
    $("frameImage").onerror = () => {
      if (selected === row.id) {
        frameReady = false;
        controls();
        error("Снимок не загрузился. Подтверждение этого кадра недоступно.");
      }
    };
    $("frameImage").src = row.image;
    $("frameMeta").textContent =
      `${dates.get(row.id)} ${timeFormat.format(new Date(row.at))} · ${row.camera}\n${row.width} × ${row.height} px · ${row.kind === "entry" ? "Вход" : row.kind === "exit" ? "Выход" : row.kind}`;
    $("prediction").textContent =
      row.prediction_type === "unknown"
        ? "Запись системы: неизвестный" +
          (people.get(row.suggested_person_id)
            ? ` · похож на ${people.get(row.suggested_person_id).full_name} (сходство ${row.suggested_similarity.toFixed(2)}), проверьте`
            : "")
        : `Запись системы: ${row.prediction_name || row.prediction_type} · личность не проверена`;
    $("currentDecision").textContent =
      `Решение: ${labels[state(selected)]}${current?.person_id ? " · " + people.get(current.person_id).full_name : ""}${current?.status === "confirmed" ? " · " + (current.reference_approved ? "Отобрано для эталона" : "Не отобрано для эталона") : ""}`;
    renderPeople();
    renderNameCandidates();
    renderPerson();
    controls();
  }
  function renderPeople() {
    if (journalMode()) {
      $("people").replaceChildren();
      return;
    }
    const needle = $("personSearch").value.trim().toLocaleLowerCase("ru-RU"),
      kind = $("personCategory").value;
    const matches = report.people.filter(
      (person) =>
        (person.active || $("inactive").checked) &&
        (!kind || person.person_type === kind) &&
        (!needle ||
          `${person.full_name} ${person.id}`
            .toLocaleLowerCase("ru-RU")
            .includes(needle)),
    );
    $("people").replaceChildren();
    for (const person of matches.slice(0, peopleLimit)) {
      const button = document.createElement("button");
      button.className = "person-option";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(person.id === personId));
      button.dataset.person = person.id;
      const image = document.createElement(
        person.references.length ? "img" : "span",
      );
      if (person.references.length) {
        image.src = person.references[0].image;
        image.alt = "Эталон";
        image.loading = "lazy";
      } else image.className = "avatar";
      const text = document.createElement("span");
      text.textContent = person.full_name;
      const sub = document.createElement("small");
      sub.textContent = `${category(person)} · ${person.references.length} эталон(ов)${person.active ? "" : " · неактивен"}`;
      text.append(sub);
      button.append(image, text);
      button.onclick = () => {
        personId = person.id;
        $("verified").checked = false;
        renderPeople();
        renderPerson();
        controls();
      };
      $("people").append(button);
    }
    if (!matches.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Люди не найдены.";
      $("people").append(p);
    }
    $("morePeople").hidden = matches.length <= peopleLimit;
  }
  function zoom(image, caption) {
    $("largeImage").src = image;
    $("zoomCaption").textContent = caption;
    $("lightbox").showModal();
  }
  function renderNameCandidates() {
    const eligible =
      journalMode() &&
      !journalPerson() &&
      !!events.get(selected)?.prediction_name;
    $("nameCandidateSection").hidden = !eligible;
    $("nameCandidates").replaceChildren();
    if (!eligible) return;
    const candidates = nameCandidates();
    $("nameCandidateCount").textContent = String(candidates.length);
    $("noNameCandidates").hidden = !!candidates.length;
    for (const { person, shared, exact } of candidates) {
      const button = document.createElement("button");
      button.className = "person-option";
      button.dataset.candidate = person.id;
      button.setAttribute("role", "option");
      button.setAttribute(
        "aria-selected",
        String(person.id === nameCandidateId),
      );
      const image = document.createElement(
        person.references.length ? "img" : "span",
      );
      if (person.references.length) {
        image.src = person.references[0].image;
        image.alt = "Эталон кандидата из ERP";
        image.loading = "lazy";
      } else image.className = "avatar";
      const text = document.createElement("span");
      text.textContent = person.full_name;
      const details = document.createElement("small");
      details.textContent = `${exact ? "Совпадают части ФИО" : `Совпадают ${shared} части ФИО`} · ${category(person)} · ${person.references.length} эталон(ов) · ${person.id}`;
      text.append(details);
      button.append(image, text);
      button.onclick = () => {
        nameCandidateId = person.id;
        $("verified").checked = false;
        renderNameCandidates();
        renderPerson();
        controls();
      };
      $("nameCandidates").append(button);
    }
  }
  function referenceNotice() {
    const person = displayedPerson();
    if (unknownReview && !person) return "Человек не выбран. Личность не подтверждена.";
    if (!person)
      return nameCandidates().length
        ? "Есть кандидаты по тексту ФИО. Человек ещё не выбран и не подтверждён."
        : "Имя сохранено в журнале, но связи с карточкой нет. Подтверждение недоступно.";
    if (!person.active) return "Карточка неактивна. Подтверждение недоступно.";
    if (!person.references.length)
      return "Эталон отсутствует. Подтверждение недоступно.";
    if (unknownReview) return "Карточка выбрана вручную · личность ещё не подтверждена";
    return selectedNameCandidate()
      ? "Кандидат по тексту ФИО · личность не подтверждена"
      : "Имя из журнала · не подтверждено";
  }
  function renderPerson() {
    const person = displayedPerson();
    $("personName").textContent = person
      ? person.full_name
      : journalMode()
        ? "Карточка по ID не найдена"
        : "Человек не выбран";
    $("personId").textContent = person
      ? `${category(person)} · ${person.id}${person.active ? "" : " · карточка неактивна"}`
      : "";
    $("references").replaceChildren();
    $("noReferences").hidden = !person || !!person.references.length;
    if (journalMode() || unknownReview) {
      referenceReady = false;
      referenceGeneration++;
      clearImage($("referenceImage"));
      $("journalWarning").textContent = referenceNotice();
      $("referenceHeading").textContent = unknownReview || selectedNameCandidate()
        ? "Эталон выбранной карточки ERP"
        : "Эталон по ID из журнала";
      $("journalQuestion").textContent = unknownReview || selectedNameCandidate()
        ? "На кадре выбранный человек из ERP?"
        : "На кадре человек, указанный в журнале?";
      $("referenceImage").hidden = !person?.references.length;
      $("zoomReference").disabled = !person?.references.length;
    }
    for (const ref of person?.references || []) {
      const button = document.createElement("button");
      button.className = "ref";
      button.title = journalMode() || unknownReview ? "Показать эталон" : "Увеличить эталон";
      button.setAttribute("aria-label", button.title);
      const image = document.createElement("img");
      image.src = ref.image;
      image.alt = "Существующий эталон";
      button.append(image);
      button.onclick = () =>
        journalMode() || unknownReview
          ? showReference(ref, button)
          : zoom(ref.image, person.full_name + " · существующий эталон");
      $("references").append(button);
    }
    if ((journalMode() || unknownReview) && person?.references.length)
      showReference(person.references[0], $("references").firstElementChild);
  }
  function showReference(ref, button) {
    const generation = ++referenceGeneration;
    referenceReady = false;
    clearImage($("referenceImage"));
    $("referenceImage").onload = () => {
      if (generation !== referenceGeneration) return;
      referenceReady = true;
      $("journalWarning").textContent = referenceNotice();
      controls();
    };
    $("referenceImage").onerror = () => {
      if (generation !== referenceGeneration) return;
      referenceReady = false;
      $("journalWarning").textContent =
        "Эталон не загрузился. Подтверждение недоступно.";
      controls();
    };
    for (const item of $("references").children)
      item.setAttribute("aria-pressed", String(item === button));
    $("referenceImage").src = ref.image;
    controls();
  }
  function setMode() {
    const journal = journalMode();
    document.body.classList.toggle("journal-mode", journal);
    $("personPicker").hidden = journal;
    $("journalReference").hidden = !(journal || unknownReview);
    $("journalQuestion").hidden = !(journal || unknownReview);
    $("rejectReason").hidden = journal;
    $("source").value = unknownReview ? "unknown" : journal ? "known" : "";
    $("source").disabled = journal || unknownReview;
    $("confirmLabel").textContent = journal || unknownReview ? "Да" : "Подтвердить фото";
    $("unsureLabel").textContent = journal || unknownReview ? "Не уверен" : "Спорно";
    $("rejectLabel").textContent = journal || unknownReview ? "Нет" : "Отклонить";
    $("verifyLabel").textContent = journal
      ? "Снимок пригоден как дополнительный эталон"
      : "Личность и пригодность снимка подтверждаю";
  }
  function controls() {
    $("confirm").disabled =
      storageBroken ||
      !selected ||
      !frameReady ||
      !displayedPerson()?.active ||
      (unknownReview && !referenceReady) ||
      (journalMode() ? !referenceReady : !$("verified").checked);
    $("verified").disabled =
      storageBroken ||
      !selected ||
      !displayedPerson()?.active ||
      ((journalMode() || unknownReview) && !referenceReady);
    $("unsure").disabled = $("reject").disabled = storageBroken || !selected;
    if (unknownReview && (!$("rejectReason").value ||
        ($("rejectReason").value === "journal_not_confirmed" && !displayedPerson())))
      $("reject").disabled = true;
    $("reset").disabled = storageBroken || !decisions.has(selected);
    $("zoom").disabled = !selected;
  }
  function decide(nextState) {
    if (
      storageBroken ||
      !selected ||
      (nextState === "confirmed" && $("confirm").disabled)
    )
      return;
    const id = selected,
      row = events.get(id),
      previous = decisions.get(id) || null;
    history.push({ id, previous });
    history = history.slice(-100);
    if (nextState === "unreviewed") decisions.delete(id);
    else
      decisions.set(id, {
        status: nextState,
        person_id: nextState === "confirmed" ? displayedPerson().id : null,
        image_sha256: row.sha256,
        human_confirmed: nextState === "confirmed",
        reason:
          nextState === "rejected"
            ? journalMode()
              ? "journal_not_confirmed"
              : $("rejectReason").value
            : null,
        decided_at: new Date().toISOString(),
        review_method: journalMode()
          ? selectedNameCandidate()
            ? "name_candidate"
            : "journal_label"
          : "manual",
        journal_person_id: journalMode() ? (journalPerson()?.id ?? null) : null,
        reference_approved: nextState === "confirmed" && $("verified").checked,
        ...(journalMode() && selectedNameCandidate()
          ? { name_candidate_id: nameCandidateId }
          : {}),
      });
    const index = filtered.findIndex((event) => event.id === id);
    const next = filtered[index + 1]?.id || filtered[index - 1]?.id || null;
    save();
    rebuild(next);
  }
  function undo() {
    if (storageBroken || !history.length) return;
    const previous = history.pop();
    const method =
      previous.previous?.review_method ??
      decisions.get(previous.id)?.review_method ??
      "manual";
    if (previous.previous) decisions.set(previous.id, previous.previous);
    else decisions.delete(previous.id);
    status = "all";
    $("reviewMode").value =
      method === "journal_label" || method === "name_candidate"
        ? "journal"
        : "manual";
    setMode();
    for (const id of ["camera", "date", "size"]) $(id).value = "";
    save();
    rebuild(previous.id);
  }
  function downloadJson(value, prefix) {
    const blob = new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob),
      link = document.createElement("a");
    link.href = url;
    link.download = `${prefix}-${report.snapshot_id.slice(0, 12)}-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function download() {
    downloadJson(session(), "photo-review");
    dirtyExport = false;
    $("saveState").textContent = "Экспорт JSON подготовлен";
  }
  function importSession(value, replaceConflicts = false) {
    const incoming = validateSession(value);
    const same = (a, b) =>
      a.status === b.status &&
      a.person_id === b.person_id &&
      a.reason === b.reason &&
      a.review_method === b.review_method &&
      a.journal_person_id === b.journal_person_id &&
      a.name_candidate_id === b.name_candidate_id &&
      a.reference_approved === b.reference_approved;
    const conflicts = [...incoming].filter(
      ([id, decision]) =>
        decisions.has(id) && !same(decisions.get(id), decision),
    );
    if (conflicts.length && !replaceConflicts)
      throw new Error(`В файле ${conflicts.length} конфликтующих решений.`);
    const merged = new Map(decisions);
    for (const [id, decision] of incoming) merged.set(id, decision);
    decisions = merged;
    history = [];
    storageBroken = false;
    error("");
    save();
    rebuild();
    return incoming.size;
  }
  $("importButton").onclick = () => $("importFile").click();
  $("importFile").onchange = async () => {
    const file = $("importFile").files[0];
    try {
      if (!file) return;
      if (file.size > 20 * 1024 * 1024)
        throw new Error("Файл превышает 20 МБ.");
      const value = JSON.parse(await file.text());
      validateSession(value);
      pendingImport = value;
      try {
        importSession(value);
      } catch (problem) {
        if (!String(problem.message).includes("конфликтующих")) throw problem;
        if (
          window.confirm(
            problem.message + " Заменить эти решения импортированными?",
          )
        )
          importSession(pendingImport, true);
      }
    } catch (problem) {
      error("Импорт не выполнен: " + problem.message);
    } finally {
      pendingImport = null;
      $("importFile").value = "";
    }
  };
  $("exportButton").onclick = download;
  $("backupButton").onclick = () => {
    if (backupSession)
      downloadJson(backupSession, "photo-review-before-name-search");
  };
  $("undo").onclick = undo;
  $("confirm").onclick = () => decide("confirmed");
  $("unsure").onclick = () => decide("unsure");
  $("reject").onclick = () => decide("rejected");
  $("reset").onclick = () => decide("unreviewed");
  $("verified").onchange = controls;
  $("rejectReason").onchange = controls;
  $("reviewMode").onchange = () => {
    setMode();
    rebuild(selected);
  };
  $("zoomReference").onclick = () => {
    if (referenceReady)
      zoom(
        $("referenceImage").src,
        displayedPerson().full_name +
          (selectedNameCandidate()
            ? " · выбранная карточка ERP"
            : " · эталон по ID из журнала"),
      );
  };
  for (const id of ["camera", "date", "source", "size", "sort"])
    $(id).onchange = () => rebuild(null);
  for (const button of $("tabs").children)
    button.onclick = () => {
      status = button.dataset.status;
      rebuild(null);
    };
  for (const [id, offset] of [
    ["prevPage", -1],
    ["nextPage", 1],
  ])
    $(id).onclick = () => {
      page += offset;
      selected = filtered[page * pageSize]?.id || null;
      renderQueue();
      inspect();
      $("queue").scrollTop = 0;
    };
  for (const [id, offset] of [
    ["prevFrame", -1],
    ["nextFrame", 1],
  ])
    $(id).onclick = () => {
      const index = filtered.findIndex((row) => row.id === selected) + offset;
      if (filtered[index]) {
        selected = filtered[index].id;
        page = Math.floor(index / pageSize);
        renderQueue();
        inspect();
      }
    };
  $("personSearch").oninput = () => {
    peopleLimit = 40;
    renderPeople();
  };
  for (const id of ["personCategory", "inactive"])
    $(id).onchange = () => {
      peopleLimit = 40;
      renderPeople();
    };
  $("morePeople").onclick = () => {
    peopleLimit += 40;
    renderPeople();
  };
  $("zoom").onclick = () => {
    const row = events.get(selected);
    if (row) zoom(row.image, $("frameMeta").textContent);
  };
  $("closeZoom").onclick = () => $("lightbox").close();
  window.addEventListener("beforeunload", (event) => {
    if (dirtyExport) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  // Expose only local review operations for deterministic offline QA.
  window.photoReview = {
    exportSession: session,
    importSession,
    validateSession,
    storageKey: key,
    nameCandidatesFor: (id) =>
      events.has(id)
        ? nameCandidates(events.get(id)).map(({ person, shared, exact }) => ({
            id: person.id,
            shared,
            exact,
          }))
        : [],
  };
  if (unknownReview) {
    $("reviewMode").replaceChildren(new Option("Неизвестные: ручной выбор", "manual"));
    $("sort").value = report.events.some((row) => row.suggested_person_id)
      ? "similar"
      : "largest";
    $("journalCount").textContent = "Личность не указана в журнале";
    $("missingInfo").textContent = report.since
      ? `Неизвестные кадры с ${report.since}, похожие на кого-то из базы (от ${report.min_similarity})`
      : "Только доступные снимки из копии от 15.10.2026";
    const rejectPlaceholder = new Option("Причина отказа", "", true, true);
    rejectPlaceholder.disabled = true;
    $("rejectReason").prepend(rejectPlaceholder, new Option("Это не выбранный человек", "journal_not_confirmed"));
    document.querySelector("h1").textContent = "Проверка неизвестных кадров";
    document.title = "Vision Office · Неизвестные кадры";
    $("referenceImage").alt = "Эталон выбранной вручную карточки";
    document.body.classList.add("unknown-review");
  }
  setMode();
  rebuild();
})();
