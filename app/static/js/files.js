/* Page « Mes fichiers » : upload, suppression, partage. Aucune URL en dur : tout part de data-base-url. */
(function () {
  "use strict";

  const root = document.querySelector(".files-page");
  if (!root) return;

  const baseUrl = root.dataset.baseUrl; // "/" en local, "/file-transfer/" derrière le reverse proxy
  const maxBytes = Number(root.dataset.maxBytes) || 0;
  const toast = document.getElementById("toast");
  let toastTimer = null;

  /* ── Toast ── */
  function showToast(message, isError = false, duration = 3000) {
    toast.textContent = message;
    toast.className = "toast" + (isError ? " toast--error" : "") + " show";
    clearTimeout(toastTimer);
    if (duration) toastTimer = setTimeout(() => toast.classList.remove("show"), duration);
  }

  /* ── Appel API : renvoie le JSON, ou lève une Error au message affichable ── */
  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(baseUrl + path, {
        ...options,
        headers: { Accept: "application/json", ...(options.headers || {}) },
      });
    } catch (_) {
      throw new Error("Connexion au serveur impossible.");
    }
    if (res.status === 401) throw new Error("Session expirée : rechargez la page pour vous reconnecter.");

    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* corps non JSON, géré ci-dessous */
    }
    if (!res.ok) throw new Error((data && data.error) || `Erreur ${res.status}`);
    if (data === null) throw new Error("Réponse inattendue du serveur (session expirée ?).");
    return data;
  }

  /* ── Menu « Partager avec » ── */
  function closeDropdowns(except = null) {
    document.querySelectorAll(".share-dropdown__menu.open").forEach((menu) => {
      if (menu === except) return;
      menu.classList.remove("open");
      const toggle = menu.parentElement.querySelector(".share-dropdown__toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "false");
    });
  }

  function toggleDropdown(button) {
    const menu = document.getElementById(`menu-${button.dataset.fileId}`);
    closeDropdowns(menu);
    const open = menu.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
  }

  function updatePreview(fileId) {
    const checked = document.querySelectorAll(`#menu-${fileId} .share-option__check:checked`).length;
    document.getElementById(`preview-${fileId}`).textContent = checked ? `${checked} utilisateur(s)` : "Sélectionner…";
  }

  async function toggleShare(checkbox) {
    const fileId = Number(checkbox.dataset.fileId);
    const add = checkbox.checked;
    checkbox.disabled = true;
    try {
      const data = await api("share", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: fileId, user_id: checkbox.dataset.userId, add }),
      });
      showToast(data.message || (add ? "Partagé ✓" : "Accès retiré"));
    } catch (err) {
      checkbox.checked = !add; // le serveur a refusé : on remet la case dans son état réel
      showToast(err.message, true);
    } finally {
      checkbox.disabled = false;
      updatePreview(fileId);
    }
  }

  /* ── Upload ── */
  async function uploadFile(input) {
    const file = input.files[0];
    if (!file) return;

    if (maxBytes && file.size > maxBytes) {
      showToast(`Fichier trop volumineux (maximum ${Math.round(maxBytes / 1048576)} Mo).`, true);
      input.value = "";
      return;
    }

    const body = new FormData();
    body.append("file", file);
    showToast("Envoi en cours…", false, 0);
    try {
      await api("upload", { method: "POST", body });
      showToast("Fichier uploadé ✓");
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      input.value = ""; // permet de renvoyer le même fichier
    }
  }

  /* ── Suppression ── */
  async function deleteFile(fileId) {
    if (!confirm("Supprimer ce fichier ? Les personnes avec qui il est partagé n'y auront plus accès.")) return;
    try {
      await api(String(fileId), { method: "DELETE" });
      showToast("Fichier supprimé");
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* ── Événements (délégation : pas de onclick dans le HTML) ── */
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (button) {
      if (button.dataset.action === "toggle-dropdown") toggleDropdown(button);
      else if (button.dataset.action === "delete") deleteFile(Number(button.dataset.fileId));
      return;
    }
    if (!event.target.closest(".share-dropdown")) closeDropdowns();
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches(".share-option__check")) toggleShare(event.target);
    else if (event.target.id === "file-input") uploadFile(event.target);
  });
})();
