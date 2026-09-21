/* A server-side folder browser: paths always refer to Vira's computer. */
window.FolderPicker = (() => {
  let active = null;
  let serial = 0;
  // A folder listing is a local read. Bound both the connection and response
  // body so a queued or stalled request cannot leave the dialog loading forever.
  const REQUEST_TIMEOUT_MS = 10000;

  function node(tag, cls, text) {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function button(text, cls, action) {
    const result = node('button', cls, text);
    result.type = 'button';
    result.addEventListener('click', action);
    return result;
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const cancelled = () => Object.assign(new Error('Folder request cancelled.'), {name: 'AbortError'});
    if (options.signal?.aborted) throw cancelled();
    let timer, abort;
    const interrupted = new Promise((_, reject) => {
      abort = () => {
        reject(cancelled());
        controller.abort();
      };
      options.signal?.addEventListener('abort', abort, {once: true});
      timer = setTimeout(() => {
        const message = options.method === 'POST' ?
          'Vira has not confirmed whether the folder was created. Refresh the folder list before trying again.' :
          'Vira took too long to open this folder. Try again, or choose another location.';
        reject(Object.assign(new Error(message), {name: 'TimeoutError'}));
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
    });
    try {
      return await Promise.race([interrupted, (async () => {
        const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail :
            'The folder could not be opened. Check that Vira can access it and try again.'),
          {status: response.status});
        }
        return data;
      })()]);
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', abort);
    }
  }

  function choose(options = {}) {
    // Only the most recent caller owns the popup and its result.
    if (active) active({cancelled: true});
    return new Promise((resolve) => {
      const id = `folder-picker-${++serial}`;
      const previousFocus = document.activeElement;
      let root = options.root || '';
      let listing = null;
      let pending = false;
      let creating = false;
      let finished = false;
      let generation = 0;
      let controller = null;
      let lastPath = options.path || root || '';
      const dialog = node('dialog', 'folder-picker');
      dialog.setAttribute('aria-labelledby', `${id}-title`);
      dialog.setAttribute('aria-describedby', `${id}-help`);

      const header = node('header', 'folder-picker-head');
      const heading = node('div');
      heading.appendChild(node('p', 'folder-picker-eyebrow', 'FOLDERS ON VIRA’S COMPUTER'));
      const title = node('h2', '', options.title || 'Choose a folder');
      title.id = `${id}-title`;
      heading.appendChild(title);
      const help = node('p', 'folder-picker-help', 'Open a folder, then select it.');
      help.id = `${id}-help`;
      heading.appendChild(help);
      header.appendChild(heading);
      const close = button('Close', 'folder-picker-close', () => finish({cancelled: true}));
      close.setAttribute('aria-label', 'Close folder picker');
      header.appendChild(close);
      dialog.appendChild(header);

      const breadcrumbs = node('nav', 'folder-picker-breadcrumbs');
      breadcrumbs.setAttribute('aria-label', 'Folder location');
      dialog.appendChild(breadcrumbs);

      const body = node('div', 'folder-picker-body');
      const sidebar = node('nav', 'folder-picker-places');
      sidebar.setAttribute('aria-label', 'Folder places');
      const main = node('div', 'folder-picker-main');
      body.appendChild(sidebar);
      body.appendChild(main);
      dialog.appendChild(body);

      const toolbar = node('div', 'folder-picker-toolbar');
      const up = button('Up one level', 'folder-picker-up', () => {
        if (listing?.parent && !pending) load(listing.parent);
      });
      toolbar.appendChild(up);
      const search = node('input', 'folder-picker-search');
      search.type = 'search';
      search.placeholder = 'Find a folder here';
      search.setAttribute('aria-label', 'Find a folder in the current location');
      search.autocomplete = 'off';
      toolbar.appendChild(search);
      main.appendChild(toolbar);

      const notice = node('p', 'folder-picker-notice');
      notice.hidden = true;
      notice.setAttribute('role', 'status');
      main.appendChild(notice);
      const selectionReason = node('p', 'folder-picker-notice folder-picker-selection-reason');
      selectionReason.id = `${id}-selection-reason`;
      selectionReason.hidden = true;
      selectionReason.setAttribute('role', 'status');
      main.appendChild(selectionReason);
      const status = node('p', 'folder-picker-status');
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      main.appendChild(status);
      const folders = node('div', 'folder-picker-folders');
      folders.setAttribute('aria-label', 'Folders');
      main.appendChild(folders);

      const utilities = node('div', 'folder-picker-utilities');
      const newFolder = button('New folder', '', () => {
        createForm.hidden = false;
        newFolder.hidden = true;
        createName.focus();
      });
      utilities.appendChild(newFolder);
      const hiddenLabel = node('label', 'folder-picker-hidden');
      const showHidden = node('input');
      showHidden.type = 'checkbox';
      hiddenLabel.appendChild(showHidden);
      hiddenLabel.appendChild(node('span', '', 'Show hidden folders'));
      utilities.appendChild(hiddenLabel);
      main.appendChild(utilities);
      const createReason = node('p', 'folder-picker-create-reason');
      main.appendChild(createReason);

      const createForm = node('form', 'folder-picker-create');
      createForm.hidden = true;
      const createLabel = node('label', '', 'New folder name');
      createLabel.htmlFor = `${id}-new-name`;
      const createName = node('input');
      createName.id = `${id}-new-name`;
      createName.name = 'folder_name';
      createName.type = 'text';
      createName.placeholder = 'e.g. Personal notes';
      createName.required = true;
      createName.autocomplete = 'off';
      createName.maxLength = 255;
      const createActions = node('div', 'folder-picker-create-actions');
      const createSubmit = node('button', 'folder-picker-primary', 'Create folder');
      createSubmit.type = 'submit';
      const createCancel = button('Cancel', '', () => {
        createForm.hidden = true;
        newFolder.hidden = false;
        createError.hidden = true;
        newFolder.focus();
      });
      createActions.appendChild(createSubmit);
      createActions.appendChild(createCancel);
      const createError = node('p', 'folder-picker-error');
      createError.setAttribute('role', 'alert');
      createError.hidden = true;
      createForm.appendChild(createLabel);
      createForm.appendChild(createName);
      createForm.appendChild(createActions);
      createForm.appendChild(createError);
      main.appendChild(createForm);

      const footer = node('footer', 'folder-picker-foot');
      const selection = node('div', 'folder-picker-selection');
      const selectedName = node('strong', '', 'No folder selected');
      const selectedPath = node('span');
      selection.appendChild(selectedName);
      selection.appendChild(selectedPath);
      const actions = node('div', 'folder-picker-actions');
      const cancel = button('Cancel', '', () => finish({cancelled: true}));
      const select = button('Select this folder', 'folder-picker-primary', () => {
        if (pending || creating || !listing || select.disabled) return;
        finish({path: listing.path, relative: listing.relative ?? null});
      });
      select.setAttribute('aria-describedby', selectionReason.id);
      actions.appendChild(cancel);
      actions.appendChild(select);
      footer.appendChild(selection);
      footer.appendChild(actions);
      dialog.appendChild(footer);

      function finish(value) {
        if (finished) return;
        finished = true;
        generation++;
        controller?.abort();
        window.removeEventListener('keydown', onKey, true);
        if (dialog.open) dialog.close();
        dialog.remove();
        if (active === finish) active = null;
        if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
        resolve(value);
      }

      function onKey(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          // Run before the app's document-level sheets and focus-mode handlers.
          event.stopImmediatePropagation();
          finish({cancelled: true});
        } else if (event.key === 'Tab') {
          const controls = Array.from(dialog.querySelectorAll('button, input, [tabindex]'))
            .filter((item) => !item.disabled && item.tabIndex >= 0 && item.getClientRects().length);
          const first = controls[0], last = controls[controls.length - 1];
          if (first && (event.shiftKey && document.activeElement === first ||
              !event.shiftKey && document.activeElement === last ||
              !dialog.contains(document.activeElement))) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
          }
        }
      }

      function updateControls() {
        const busy = pending || creating;
        const forbiddenRoot = root && options.allowRoot === false && listing?.relative === '.';
        select.disabled = busy || !listing || forbiddenRoot || listing.selectable === false;
        up.disabled = busy || !listing?.parent;
        search.disabled = busy || !listing;
        showHidden.disabled = busy || !listing;
        newFolder.disabled = busy || !listing?.can_create;
        createSubmit.disabled = busy;
        createCancel.disabled = busy;
        createName.disabled = busy;
        dialog.setAttribute('aria-busy', String(busy));
        folders.setAttribute('aria-busy', String(busy));
        for (const target of [sidebar, breadcrumbs, folders]) {
          for (const control of target.querySelectorAll('button')) control.disabled = busy;
        }
        selectedName.textContent = listing ? listing.name : 'No folder selected';
        selectedPath.textContent = listing ? listing.path : lastPath;
        if (forbiddenRoot) selectedName.textContent = 'Choose a folder inside this vault';
        selectionReason.textContent = listing?.selectable === false ?
          listing.selection_disabled_reason || 'This folder cannot be selected. Choose another folder.' : '';
        selectionReason.hidden = !selectionReason.textContent;
        createReason.textContent = listing && !listing.can_create ?
          listing.create_disabled_reason || 'New folders cannot be created in this location.' : '';
        createReason.hidden = !createReason.textContent;
      }

      function paintFolders() {
        folders.replaceChildren();
        if (!listing) return;
        const query = search.value.trim().toLocaleLowerCase();
        const visible = listing.folders.filter((folder) => folder.name.toLocaleLowerCase().includes(query));
        status.textContent = query ? `${visible.length} matching folder${visible.length === 1 ? '' : 's'}` :
          `${visible.length} folder${visible.length === 1 ? '' : 's'}`;
        if (!visible.length) {
          const forbiddenRoot = root && options.allowRoot === false && listing.relative === '.';
          const emptyHelp = listing.selectable === false ? 'Choose another folder.' : forbiddenRoot ? (listing.can_create ?
            'Create a folder here to select it.' : 'This vault has no folders available to select yet.') :
            (listing.can_create ? 'You can select this folder or create a new one.' : 'You can select this folder.');
          folders.appendChild(node('p', 'folder-picker-empty', query ?
            'No matching folders here. Try another name.' : `No subfolders here. ${emptyHelp}`));
        }
        for (const folder of visible) {
          const row = button('', 'folder-picker-row', () => load(folder.path));
          row.appendChild(node('span', 'folder-picker-folder-icon'));
          row.firstChild.setAttribute('aria-hidden', 'true');
          row.appendChild(node('span', 'folder-picker-folder-name', folder.name));
          const openHint = node('span', 'folder-picker-open-hint', 'Open');
          openHint.setAttribute('aria-hidden', 'true');
          row.appendChild(openHint);
          row.setAttribute('aria-label', `Open folder ${folder.name}`);
          folders.appendChild(row);
        }
      }

      function paint(data) {
        if (!data.path || !Array.isArray(data.folders)) throw new Error('The folder list could not be loaded. Try again.');
        listing = data;
        if (data.root) root = data.root;
        lastPath = data.path;
        breadcrumbs.replaceChildren();
        const crumbs = data.ancestors || [];
        for (const part of crumbs) {
          const crumb = button(part.name, '', () => load(part.path));
          if (part.path === data.path) crumb.setAttribute('aria-current', 'location');
          breadcrumbs.appendChild(crumb);
        }
        sidebar.replaceChildren(node('p', 'folder-picker-places-label', 'PLACES'));
        for (const place of data.places || []) {
          const destination = button(place.name, '', () => load(place.path));
          if (place.path === data.path) destination.setAttribute('aria-current', 'location');
          sidebar.appendChild(destination);
        }
        search.value = '';
        paintFolders();
      }

      async function load(path, fallback = false) {
        if (finished || creating) return;
        const turn = ++generation;
        controller?.abort();
        controller = new AbortController();
        pending = true;
        lastPath = path || root || '';
        listing = null;
        folders.replaceChildren();
        status.textContent = 'Opening folder...';
        notice.hidden = true;
        createForm.hidden = true;
        newFolder.hidden = false;
        createName.value = '';
        createError.hidden = true;
        updateControls();
        const query = new URLSearchParams();
        if (path) query.set('path', path);
        if (root) query.set('root', root);
        if (showHidden.checked) query.set('show_hidden', 'true');
        try {
          const suffix = query.toString();
          const data = await request('/api/folders' + (suffix ? '?' + suffix : ''), {signal: controller.signal});
          if (finished || turn !== generation) return;
          paint(data);
          if (fallback) {
            notice.textContent = 'The previous folder is unavailable. Choose a folder in this vault.';
            notice.hidden = false;
          }
        } catch (error) {
          if (finished || turn !== generation) return;
          // Missing or inaccessible saved folders can recover at their vault.
          // Connection failures and timeouts need an explicit retry, not a
          // second stalled request or a misleading "folder unavailable" notice.
          if ([400, 403, 404].includes(error.status) && root && path && path !== root && !fallback) {
            pending = false;
            return load(root, true);
          }
          status.textContent = 'This folder could not be opened';
          folders.appendChild(node('p', 'folder-picker-error', error.message));
          const recovery = node('div', 'folder-picker-recovery');
          recovery.appendChild(button('Try again', '', () => load(path)));
          recovery.appendChild(button(root ? 'Go to vault folder' : 'Go to home folder', '', () => load(root)));
          folders.appendChild(recovery);
        } finally {
          if (!finished && turn === generation) {
            pending = false;
            updateControls();
          }
        }
      }

      createForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (pending || creating || !listing?.can_create) return;
        const name = createName.value.trim();
        if (!name || name === '.' || name === '..' || /[/\\\x00-\x1f]/.test(name)) {
          createError.textContent = 'Enter a folder name without slashes.';
          createError.hidden = false;
          createName.focus();
          return;
        }
        const parent = listing.path;
        const turn = ++generation;
        controller?.abort();
        controller = new AbortController();
        creating = true;
        createError.hidden = true;
        createSubmit.textContent = 'Creating...';
        updateControls();
        try {
          const data = await request('/api/folders', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({parent, name, ...(root ? {root} : {})}),
            signal: controller.signal,
          });
          if (finished || turn !== generation) return;
          paint(data);
          createForm.hidden = true;
          newFolder.hidden = false;
          createName.value = '';
          notice.textContent = 'Folder created. Select it to continue.';
          notice.hidden = false;
        } catch (error) {
          if (finished || turn !== generation) return;
          createError.textContent = error.message;
          if (error.name === 'TimeoutError') {
            createError.appendChild(button('Refresh folders', '', () => load(parent)));
          }
          createError.hidden = false;
        } finally {
          creating = false;
          if (!finished) {
            createSubmit.textContent = 'Create folder';
            updateControls();
            (createForm.hidden ? select : createName).focus();
          }
        }
      });

      search.addEventListener('input', paintFolders);
      showHidden.addEventListener('change', () => load(lastPath));
      dialog.addEventListener('cancel', (event) => {
        event.preventDefault();
        finish({cancelled: true});
      });
      dialog.addEventListener('close', () => finish({cancelled: true}));
      dialog.addEventListener('keydown', (event) => event.stopPropagation());
      document.body.appendChild(dialog);
      active = finish;
      window.addEventListener('keydown', onKey, true);
      dialog.showModal();
      close.focus();
      load(lastPath);
    });
  }

  return {choose};
})();
