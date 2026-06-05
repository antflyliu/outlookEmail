        /* global accountPaginationState, accountsCache, clearEmailSelection, closeModal, copyTextToClipboard, currentAccount, currentAccountListSource, currentEmailDetail, currentGroupId, deleteAccount, getSelectedForwardChannels, handleApiError, hideModal, invalidateAccountCaches, isTempEmailGroup, loadAccountsByGroup, loadGroups, loadTags, refreshVisibleAccountList, renderEmailList, selectedEmailIds, setModalVisible, showModal, showToast, updateBatchActionBar */

        // ==================== 批量操作 ====================

        let accountSelectionMode = false;
        let accountSelectionAnchorId = null;
        let accountSelectionDragState = null;
        let accountSelectionSuppressClickUntil = 0;
        let disabledCheckTaskLocked = false;

        function getAccountSelectionCheckboxes() {
            return Array.from(document.querySelectorAll('#accountList .account-select-checkbox'));
        }

        function getAccountSelectionCheckboxById(accountId) {
            return getAccountSelectionCheckboxes()
                .find(checkbox => String(checkbox.value) === String(accountId));
        }

        function setAccountSelectionMode(enabled) {
            accountSelectionMode = !!enabled;
            document.getElementById('accountPanel')?.classList.toggle('account-selection-mode', accountSelectionMode);
            document.querySelectorAll('.account-selection-mode-btn').forEach(button => {
                button.classList.toggle('active', accountSelectionMode);
                button.setAttribute('aria-pressed', accountSelectionMode ? 'true' : 'false');
                button.title = accountSelectionMode ? '退出批量选择' : '批量选择';
            });
            if (!accountSelectionMode) {
                accountSelectionDragState = null;
            }
        }

        function toggleAccountSelectionMode() {
            setAccountSelectionMode(!accountSelectionMode);
        }

        function setAccountSelectionAnchor(checkbox) {
            if (checkbox) {
                accountSelectionAnchorId = String(checkbox.value);
            }
        }

        function setAccountSelectionRange(fromCheckbox, toCheckbox, checked) {
            const checkboxes = getAccountSelectionCheckboxes();
            const fromIndex = checkboxes.indexOf(fromCheckbox);
            const toIndex = checkboxes.indexOf(toCheckbox);
            if (fromIndex === -1 || toIndex === -1) {
                return false;
            }

            const start = Math.min(fromIndex, toIndex);
            const end = Math.max(fromIndex, toIndex);
            for (let index = start; index <= end; index += 1) {
                checkboxes[index].checked = checked;
            }
            return true;
        }

        function applyAccountSelectionFromCheckbox(checkbox, event = null) {
            if (!checkbox) {
                return;
            }

            if (event?.shiftKey && accountSelectionAnchorId) {
                const anchor = getAccountSelectionCheckboxById(accountSelectionAnchorId);
                if (anchor && setAccountSelectionRange(anchor, checkbox, checkbox.checked)) {
                    event.preventDefault?.();
                }
            }
            setAccountSelectionAnchor(checkbox);
            updateBatchActionBar();
        }

        function handleAccountSelectionCheckboxClick(event) {
            event.stopPropagation();
            if (accountSelectionMode && Date.now() < accountSelectionSuppressClickUntil) {
                event.preventDefault();
                return;
            }
            applyAccountSelectionFromCheckbox(event.currentTarget, event);
        }

        function handleAccountRowSelectionClick(event) {
            if (Date.now() < accountSelectionSuppressClickUntil) {
                event?.preventDefault?.();
                return;
            }
            if (event?.target?.closest?.('.account-menu-wrap, .account-action-btn, .account-menu-trigger, .account-menu-panel, .account-error-btn, button, input, a')) {
                return;
            }

            const item = event.currentTarget;
            const checkbox = item?.querySelector?.('.account-select-checkbox');
            if (!checkbox) {
                return;
            }

            event?.preventDefault?.();
            if (event?.shiftKey && accountSelectionAnchorId) {
                checkbox.checked = true;
                applyAccountSelectionFromCheckbox(checkbox, event);
            } else {
                checkbox.checked = !checkbox.checked;
                applyAccountSelectionFromCheckbox(checkbox, event);
            }
        }

        function setAccountDragSelection(checkbox) {
            if (!accountSelectionDragState || !checkbox) {
                return;
            }
            const accountId = String(checkbox.value);
            if (accountSelectionDragState.visitedIds.has(accountId)) {
                return;
            }
            accountSelectionDragState.visitedIds.add(accountId);
            checkbox.checked = accountSelectionDragState.targetChecked;
            setAccountSelectionAnchor(checkbox);
            updateBatchActionBar();
        }

        function handleAccountSelectionPointerDown(event) {
            if (!accountSelectionMode || event.button !== 0) {
                return;
            }
            const startedOnCheckbox = !!event.target.closest('.account-select-checkbox');
            if (!startedOnCheckbox && event.target.closest('.account-menu-wrap, .account-action-btn, .account-menu-trigger, .account-menu-panel, .account-error-btn, button, input, a')) {
                return;
            }

            const item = event.target.closest('.account-item');
            const checkbox = item?.querySelector?.('.account-select-checkbox');
            if (!checkbox) {
                return;
            }

            event.preventDefault();
            accountSelectionSuppressClickUntil = Date.now() + 350;
            accountSelectionDragState = {
                pointerId: event.pointerId,
                targetChecked: !checkbox.checked,
                visitedIds: new Set()
            };
            document.getElementById('accountList')?.setPointerCapture?.(event.pointerId);
            setAccountDragSelection(checkbox);
        }

        function handleAccountSelectionPointerMove(event) {
            if (!accountSelectionDragState || event.pointerId !== accountSelectionDragState.pointerId) {
                return;
            }
            event.preventDefault();
            const element = document.elementFromPoint(event.clientX, event.clientY);
            const item = element?.closest?.('#accountList .account-item');
            const checkbox = item?.querySelector?.('.account-select-checkbox');
            setAccountDragSelection(checkbox);
        }

        function handleAccountSelectionPointerEnd(event) {
            if (!accountSelectionDragState || event.pointerId !== accountSelectionDragState.pointerId) {
                return;
            }
            document.getElementById('accountList')?.releasePointerCapture?.(event.pointerId);
            accountSelectionDragState = null;
        }

        function initAccountSelectionGestures() {
            const accountList = document.getElementById('accountList');
            if (!accountList || accountList.dataset.boundSelectionGestures) {
                return;
            }
            accountList.dataset.boundSelectionGestures = 'true';
            accountList.addEventListener('pointerdown', handleAccountSelectionPointerDown);
            accountList.addEventListener('pointermove', handleAccountSelectionPointerMove);
            accountList.addEventListener('pointerup', handleAccountSelectionPointerEnd);
            accountList.addEventListener('pointercancel', handleAccountSelectionPointerEnd);
            accountList.addEventListener('scroll', positionAccountBatchActionBar, { passive: true });
            window.addEventListener('resize', positionAccountBatchActionBar);
        }

        function resetAccountBatchActionBarPosition() {
            const bar = document.getElementById('batchActionBar');
            if (!bar) return;
            bar.style.removeProperty('--batch-action-top');
            bar.style.removeProperty('--batch-action-max-width');
        }

        function positionAccountBatchActionBar() {
            const bar = document.getElementById('batchActionBar');
            const panel = document.getElementById('accountPanel');
            const firstChecked = document.querySelector('#accountList .account-select-checkbox:checked');
            const firstSelectedItem = firstChecked?.closest('.account-item');

            if (!bar || !panel || !firstSelectedItem || !window.matchMedia('(min-width: 769px)').matches) {
                resetAccountBatchActionBarPosition();
                return;
            }

            const panelRect = panel.getBoundingClientRect();
            const itemRect = firstSelectedItem.getBoundingClientRect();
            const availableWidth = Math.max(260, window.innerWidth - panelRect.right - 16);
            bar.style.setProperty('--batch-action-max-width', `${Math.min(560, Math.round(availableWidth))}px`);

            const barHeight = bar.offsetHeight || 0;
            const minTop = 8;
            const maxTop = Math.max(minTop, panelRect.height - barHeight - 12);
            const selectedTop = itemRect.top - panelRect.top;
            const top = Math.min(Math.max(selectedTop, minTop), maxTop);

            bar.style.setProperty('--batch-action-top', `${Math.round(top)}px`);
        }

        // 更新批量操作栏状态
        function updateBatchActionBar() {
            const checked = Array.from(document.querySelectorAll('.account-select-checkbox:checked'));
            const allCheckboxes = document.querySelectorAll('#accountList .account-select-checkbox');
            const bar = document.getElementById('batchActionBar');
            const countSpan = document.getElementById('selectedCount');
            const selectAllBtn = document.getElementById('accountSelectAllBtn');
            const selectInactiveBtn = document.getElementById('accountSelectInactiveBtn');
            const batchRefreshBtn = document.getElementById('batchRefreshTokensBtn');
            const batchActivateBtn = document.getElementById('batchActivateAccountsBtn');
            const batchDeactivateBtn = document.getElementById('batchDeactivateAccountsBtn');
            const batchCopyBtn = document.getElementById('batchCopyEmailsBtn');
            const batchEnableForwardingBtn = document.getElementById('batchEnableForwardingBtn');
            const batchDisableForwardingBtn = document.getElementById('batchDisableForwardingBtn');
            const batchAddTagBtn = document.getElementById('batchAddTagBtn');
            const batchRemoveTagBtn = document.getElementById('batchRemoveTagBtn');
            const batchMoveGroupBtn = document.getElementById('batchMoveGroupBtn');
            const batchDeleteBtn = document.getElementById('batchDeleteAccountsBtn');
            const panel = document.getElementById('accountPanel');
            const refreshableChecked = checked.filter(cb => cb.dataset.refreshable === 'true');
            const inactiveChecked = checked.filter(cb => cb.dataset.accountStatus === 'inactive');
            const activeChecked = checked.filter(cb => cb.dataset.accountStatus !== 'inactive');
            const enableForwardingChecked = checked.filter(cb => cb.dataset.forwardEnabled !== 'true');
            const disableForwardingChecked = checked.filter(cb => cb.dataset.forwardEnabled === 'true');
            const inactiveLoaded = Array.from(allCheckboxes).filter(cb => cb.dataset.accountStatus === 'inactive');
            const isStatusUpdating = batchActivateBtn?.dataset.loading === 'true'
                || batchDeactivateBtn?.dataset.loading === 'true';
            const isForwardingUpdating = batchEnableForwardingBtn?.dataset.loading === 'true'
                || batchDisableForwardingBtn?.dataset.loading === 'true';
            const isTempContext = !!isTempEmailGroup;
            const loadedAccountCount = allCheckboxes.length;
            const totalAccountCount = Number(accountPaginationState?.total) || loadedAccountCount;
            const isPartialPageLoaded = !isTempContext && totalAccountCount > loadedAccountCount;
            const loadedScopeSuffix = isPartialPageLoaded
                ? `（已加载 ${loadedAccountCount}/${totalAccountCount}）`
                : '';

            if (batchRefreshBtn) batchRefreshBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (selectInactiveBtn) selectInactiveBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchActivateBtn) batchActivateBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchDeactivateBtn) batchDeactivateBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchEnableForwardingBtn) batchEnableForwardingBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchDisableForwardingBtn) batchDisableForwardingBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchMoveGroupBtn) batchMoveGroupBtn.style.display = isTempContext ? 'none' : 'inline-flex';
            if (batchAddTagBtn) batchAddTagBtn.style.display = 'inline-flex';
            if (batchRemoveTagBtn) batchRemoveTagBtn.style.display = 'inline-flex';
            if (batchDeleteBtn) batchDeleteBtn.style.display = 'inline-flex';
            if (selectAllBtn) {
                const allLoadedChecked = loadedAccountCount > 0 && checked.length === loadedAccountCount;
                const scopeLabel = isPartialPageLoaded ? '已加载' : '';
                selectAllBtn.textContent = allLoadedChecked
                    ? `取消全选${scopeLabel}`
                    : `全选${scopeLabel}`;
            }
            if (selectInactiveBtn) {
                selectInactiveBtn.disabled = !isTempContext && inactiveLoaded.length === 0;
                selectInactiveBtn.textContent = inactiveLoaded.length > 0
                    ? `选择停用 (${inactiveLoaded.length})`
                    : '选择停用';
                selectInactiveBtn.title = inactiveLoaded.length > 0
                    ? '选择当前已加载列表中的停用账号'
                    : '当前已加载列表中没有停用账号';
            }

            if (checked.length > 0) {
                bar.style.display = 'flex';
                panel?.classList.add('batch-toolbar-active');
                countSpan.textContent = isTempContext
                    ? `已选 ${checked.length} 项`
                    : (refreshableChecked.length > 0 && refreshableChecked.length !== checked.length
                    ? `已选 ${checked.length} 项，可刷新 ${refreshableChecked.length} 项${loadedScopeSuffix}`
                    : `已选 ${checked.length} 项${loadedScopeSuffix}`);
                if (batchRefreshBtn) {
                    const isRefreshing = batchRefreshBtn.dataset.loading === 'true';
                    batchRefreshBtn.disabled = refreshableChecked.length === 0 || isRefreshing;
                    batchRefreshBtn.title = refreshableChecked.length === 0
                        ? '所选账号中没有可刷新的 Outlook 账号'
                        : '';
                    if (!isRefreshing) {
                        batchRefreshBtn.textContent = refreshableChecked.length > 0
                            ? `刷新 Token${refreshableChecked.length !== checked.length ? ` (${refreshableChecked.length})` : ''}`
                            : '刷新 Token';
                    }
                }
                if (batchActivateBtn) {
                    batchActivateBtn.disabled = inactiveChecked.length === 0 || isStatusUpdating;
                    batchActivateBtn.title = inactiveChecked.length === 0
                        ? '所选账号中没有停用账号'
                        : '';
                    if (batchActivateBtn.dataset.loading !== 'true') {
                        batchActivateBtn.textContent = inactiveChecked.length > 0
                            ? `启用账号${inactiveChecked.length !== checked.length ? ` (${inactiveChecked.length})` : ''}`
                            : '启用账号';
                    }
                }
                if (batchDeactivateBtn) {
                    batchDeactivateBtn.disabled = activeChecked.length === 0 || isStatusUpdating;
                    batchDeactivateBtn.title = activeChecked.length === 0
                        ? '所选账号已全部停用'
                        : '';
                    if (batchDeactivateBtn.dataset.loading !== 'true') {
                        batchDeactivateBtn.textContent = activeChecked.length > 0
                            ? `停用账号${activeChecked.length !== checked.length ? ` (${activeChecked.length})` : ''}`
                            : '停用账号';
                    }
                }
                if (batchCopyBtn) {
                    const isCopying = batchCopyBtn.dataset.loading === 'true';
                    batchCopyBtn.disabled = checked.length === 0 || isCopying;
                    if (!isCopying) {
                        batchCopyBtn.textContent = isTempContext
                            ? (checked.length > 1 ? `复制邮箱 (${checked.length})` : '复制邮箱')
                            : (checked.length > 1 ? `复制邮箱+别名 (${checked.length})` : '复制邮箱+别名');
                    }
                }
                if (batchEnableForwardingBtn) {
                    batchEnableForwardingBtn.disabled = enableForwardingChecked.length === 0 || isForwardingUpdating;
                    batchEnableForwardingBtn.title = enableForwardingChecked.length === 0
                        ? '所选账号已全部开启转发'
                        : '';
                    if (batchEnableForwardingBtn.dataset.loading !== 'true') {
                        batchEnableForwardingBtn.textContent = enableForwardingChecked.length > 0
                            ? `开启转发${enableForwardingChecked.length !== checked.length ? ` (${enableForwardingChecked.length})` : ''}`
                            : '开启转发';
                    }
                }
                if (batchDisableForwardingBtn) {
                    batchDisableForwardingBtn.disabled = disableForwardingChecked.length === 0 || isForwardingUpdating;
                    batchDisableForwardingBtn.title = disableForwardingChecked.length === 0
                        ? '所选账号已全部取消转发'
                        : '';
                    if (batchDisableForwardingBtn.dataset.loading !== 'true') {
                        batchDisableForwardingBtn.textContent = disableForwardingChecked.length > 0
                            ? `取消转发${disableForwardingChecked.length !== checked.length ? ` (${disableForwardingChecked.length})` : ''}`
                            : '取消转发';
                    }
                }
                if (batchDeleteBtn) {
                    const isDeleting = batchDeleteBtn.dataset.loading === 'true';
                    batchDeleteBtn.disabled = isDeleting;
                    if (!isDeleting) {
                        batchDeleteBtn.textContent = checked.length > 1 ? `删除 (${checked.length})` : '删除';
                    }
                }
                positionAccountBatchActionBar();
            } else {
                bar.style.display = 'none';
                panel?.classList.remove('batch-toolbar-active');
                resetAccountBatchActionBarPosition();
                if (batchRefreshBtn) {
                    batchRefreshBtn.disabled = false;
                    batchRefreshBtn.dataset.loading = 'false';
                    batchRefreshBtn.textContent = '刷新 Token';
                    batchRefreshBtn.title = '';
                }
                if (batchCopyBtn) {
                    batchCopyBtn.disabled = false;
                    batchCopyBtn.dataset.loading = 'false';
                    batchCopyBtn.textContent = isTempContext ? '复制邮箱' : '复制邮箱+别名';
                    batchCopyBtn.title = '';
                }
                if (batchActivateBtn) {
                    batchActivateBtn.disabled = false;
                    batchActivateBtn.dataset.loading = 'false';
                    batchActivateBtn.textContent = '启用账号';
                    batchActivateBtn.title = '';
                }
                if (batchDeactivateBtn) {
                    batchDeactivateBtn.disabled = false;
                    batchDeactivateBtn.dataset.loading = 'false';
                    batchDeactivateBtn.textContent = '停用账号';
                    batchDeactivateBtn.title = '';
                }
                if (batchEnableForwardingBtn) {
                    batchEnableForwardingBtn.disabled = false;
                    batchEnableForwardingBtn.dataset.loading = 'false';
                    batchEnableForwardingBtn.textContent = '开启转发';
                    batchEnableForwardingBtn.title = '';
                }
                if (batchDisableForwardingBtn) {
                    batchDisableForwardingBtn.disabled = false;
                    batchDisableForwardingBtn.dataset.loading = 'false';
                    batchDisableForwardingBtn.textContent = '取消转发';
                    batchDisableForwardingBtn.title = '';
                }
                if (batchDeleteBtn) {
                    batchDeleteBtn.disabled = false;
                    batchDeleteBtn.dataset.loading = 'false';
                    batchDeleteBtn.textContent = '删除';
                }
            }
        }

        function toggleSelectAllAccounts() {
            const checkboxes = Array.from(document.querySelectorAll('#accountList .account-select-checkbox'));
            if (!checkboxes.length) return;

            const shouldClear = checkboxes.every(cb => cb.checked);
            checkboxes.forEach(cb => {
                cb.checked = !shouldClear;
            });
            updateBatchActionBar();
        }

        function selectInactiveAccounts() {
            const checkboxes = Array.from(document.querySelectorAll('#accountList .account-select-checkbox'));
            if (!checkboxes.length || isTempEmailGroup) return;

            const inactiveCheckboxes = checkboxes.filter(cb => cb.dataset.accountStatus === 'inactive');
            if (!inactiveCheckboxes.length) {
                showToast('当前已加载列表中没有停用账号', 'error');
                return;
            }

            checkboxes.forEach(cb => {
                cb.checked = cb.dataset.accountStatus === 'inactive';
            });
            setAccountSelectionMode(true);
            updateBatchActionBar();
            showToast(`已选择 ${inactiveCheckboxes.length} 个停用账号`, 'success');
        }

        function clearAccountSelection() {
            document.querySelectorAll('#accountList .account-select-checkbox').forEach(cb => {
                cb.checked = false;
            });
            accountSelectionAnchorId = null;
            updateBatchActionBar();
        }

        function getSelectedAccountIds() {
            return Array.from(document.querySelectorAll('#accountList .account-select-checkbox:checked'))
                .map(cb => parseInt(cb.value, 10))
                .filter(Number.isFinite);
        }

        function getSelectedAccounts() {
            const selectedIds = new Set(getSelectedAccountIds());
            if (!selectedIds.size) {
                return [];
            }

            return (Array.isArray(currentAccountListSource) ? currentAccountListSource : [])
                .filter(account => selectedIds.has(parseInt(account.id, 10)));
        }

        async function copySelectedAccountsWithAliases() {
            const btn = document.getElementById('batchCopyEmailsBtn');
            if (!btn || btn.disabled) return;

            const selectedAccounts = getSelectedAccounts();
            if (!selectedAccounts.length) {
                showToast('请先选择要复制的邮箱', 'error');
                return;
            }

            const emailSet = new Set();
            selectedAccounts.forEach(account => {
                const candidates = [account.email].concat(Array.isArray(account.aliases) ? account.aliases : []);
                candidates
                    .map(value => String(value || '').trim())
                    .filter(Boolean)
                    .forEach(email => emailSet.add(email));
            });

            const emailList = Array.from(emailSet);
            if (!emailList.length) {
                showToast('所选账号没有可复制的邮箱', 'error');
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '复制中...';

            try {
                await copyTextToClipboard(emailList.join('\n'), `已复制 ${emailList.length} 个邮箱地址`);
            } finally {
                btn.dataset.loading = 'false';
                updateBatchActionBar();
            }
        }

        async function refreshSelectedAccounts() {
            const btn = document.getElementById('batchRefreshTokensBtn');
            if (!btn || btn.disabled) return;

            const checked = Array.from(document.querySelectorAll('#accountList .account-select-checkbox:checked'));
            const accountIds = getSelectedAccountIds();
            const refreshableCount = checked.filter(cb => cb.dataset.refreshable === 'true').length;

            if (!accountIds.length) {
                showToast('请先选择要刷新的邮箱', 'error');
                return;
            }
            if (!refreshableCount) {
                showToast('所选账号中没有可刷新的 Outlook 账号', 'error');
                return;
            }
            if (!(await showConfirmModal(`确定要刷新所选 ${accountIds.length} 个邮箱的 Token 吗？`, { title: '批量刷新 Token', confirmText: '确认刷新', danger: false }))) {
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '刷新中...';

            try {
                const response = await fetch('/api/accounts/refresh-selected', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ account_ids: accountIds })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, '批量刷新失败');
                    return;
                }

                const toastType = data.failed_count > 0 || data.skipped_count > 0 ? 'warning' : 'success';
                showToast(
                    `批量刷新完成：成功 ${data.success_count}，失败 ${data.failed_count}，跳过 ${data.skipped_count}`,
                    toastType
                );

                if (data.failed_count > 0) {
                    await openRefreshModalWithStatus('failed');
                } else {
                    loadRefreshStats();
                }

                clearAccountSelection();
                await refreshVisibleAccountList(true);
            } catch (error) {
                showToast('批量刷新请求失败', 'error');
            } finally {
                btn.dataset.loading = 'false';
                updateBatchActionBar();
            }
        }

        async function updateStatusForSelectedAccounts(targetStatus) {
            const normalizedStatus = targetStatus === 'inactive' ? 'inactive' : 'active';
            const btn = document.getElementById(
                normalizedStatus === 'active' ? 'batchActivateAccountsBtn' : 'batchDeactivateAccountsBtn'
            );
            if (!btn || btn.disabled) return;

            const checked = Array.from(document.querySelectorAll('#accountList .account-select-checkbox:checked'));
            const eligible = checked.filter(cb => {
                const currentStatus = cb.dataset.accountStatus === 'inactive' ? 'inactive' : 'active';
                return currentStatus !== normalizedStatus;
            });
            const accountIds = eligible
                .map(cb => parseInt(cb.value, 10))
                .filter(Number.isFinite);
            const actionLabel = normalizedStatus === 'active' ? '启用' : '停用';
            const loadingLabel = normalizedStatus === 'active' ? '启用中...' : '停用中...';

            if (!checked.length) {
                showToast(`请先选择要${actionLabel}的邮箱`, 'error');
                return;
            }
            if (!accountIds.length) {
                showToast(`所选账号已全部处于${actionLabel}状态`, 'error');
                return;
            }

            const skippedCount = checked.length - accountIds.length;
            const confirmMessage = skippedCount > 0
                ? `确定要${actionLabel}所选账号吗？其中 ${skippedCount} 个账号已处于目标状态，会自动跳过。`
                : `确定要${actionLabel}所选 ${accountIds.length} 个账号吗？`;
            if (!(await showConfirmModal(confirmMessage, { title: `批量${actionLabel}账号`, confirmText: `确认${actionLabel}`, danger: normalizedStatus === 'inactive' }))) {
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = loadingLabel;

            try {
                const response = await fetch('/api/accounts/batch-update-status', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        account_ids: accountIds,
                        status: normalizedStatus
                    })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, `批量${actionLabel}账号失败`);
                    return;
                }

                showToast(data.message || `已${actionLabel} ${data.updated_count || accountIds.length} 个账号`, 'success');
                invalidateAccountCaches();
                clearAccountSelection();
                loadGroups();
                await refreshVisibleAccountList(true);
            } catch (error) {
                showToast(`批量${actionLabel}账号失败`, 'error');
            } finally {
                btn.dataset.loading = 'false';
                updateBatchActionBar();
            }
        }

        async function activateSelectedAccounts() {
            await updateStatusForSelectedAccounts('active');
        }

        async function deactivateSelectedAccounts() {
            await updateStatusForSelectedAccounts('inactive');
        }

        async function updateForwardingForSelectedAccounts(targetEnabled) {
            const btn = document.getElementById(targetEnabled ? 'batchEnableForwardingBtn' : 'batchDisableForwardingBtn');
            if (!btn || btn.disabled) return;

            const checked = Array.from(document.querySelectorAll('#accountList .account-select-checkbox:checked'));
            const accountIds = checked
                .map(cb => parseInt(cb.value, 10))
                .filter(Number.isFinite);
            const eligibleCount = checked.filter(cb => (cb.dataset.forwardEnabled === 'true') !== targetEnabled).length;
            const actionLabel = targetEnabled ? '开启转发' : '取消转发';
            const loadingLabel = targetEnabled ? '开启中...' : '取消中...';
            const finishedLabel = targetEnabled ? '已全部开启转发' : '已全部取消转发';
            const skippedLabel = targetEnabled ? '已开启' : '已取消';

            if (!accountIds.length) {
                showToast(`请先选择要${actionLabel}的邮箱`, 'error');
                return;
            }
            if (!eligibleCount) {
                showToast(`所选账号${finishedLabel}`, 'error');
                return;
            }

            const skippedCount = accountIds.length - eligibleCount;
            const confirmMessage = skippedCount > 0
                ? `确定要为所选 ${accountIds.length} 个邮箱${actionLabel}吗？其中 ${skippedCount} 个${skippedLabel}账号会自动跳过。`
                : `确定要为所选 ${accountIds.length} 个邮箱${actionLabel}吗？`;
            if (!(await showConfirmModal(confirmMessage, { title: actionLabel, confirmText: '确认', danger: false }))) {
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = loadingLabel;

            try {
                const response = await fetch('/api/accounts/batch-update-forwarding', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        account_ids: accountIds,
                        forward_enabled: targetEnabled
                    })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, `批量${actionLabel}失败`);
                    return;
                }

                showToast(data.message || `已为 ${eligibleCount} 个账号${actionLabel}`, 'success');
                invalidateAccountCaches();
                clearAccountSelection();
                await refreshVisibleAccountList(true);
            } catch (error) {
                showToast(`批量${actionLabel}失败`, 'error');
            } finally {
                btn.dataset.loading = 'false';
                updateBatchActionBar();
            }
        }

        async function enableForwardingForSelectedAccounts() {
            await updateForwardingForSelectedAccounts(true);
        }

        async function disableForwardingForSelectedAccounts() {
            await updateForwardingForSelectedAccounts(false);
        }

        function resetDisabledCheckResults() {
            const summaryEl = document.getElementById('disabledCheckSummary');
            const resultsEl = document.getElementById('disabledCheckResults');
            if (summaryEl) {
                summaryEl.hidden = true;
                summaryEl.innerHTML = '';
            }
            if (resultsEl) {
                resultsEl.hidden = true;
                resultsEl.innerHTML = '';
            }
        }

        function setDisabledCheckTaskLocked(locked) {
            disabledCheckTaskLocked = !!locked;
            document.body?.classList.toggle('disabled-check-page-locked', disabledCheckTaskLocked);
            document.getElementById('disabledCheckModal')?.classList.toggle('disabled-check-modal--locked', disabledCheckTaskLocked);
            const historyBtn = document.getElementById('disabledCheckHistoryBtn');
            const recentCount = document.getElementById('disabledCheckRecentCount');
            if (historyBtn) historyBtn.disabled = disabledCheckTaskLocked;
            if (recentCount) recentCount.disabled = disabledCheckTaskLocked;
        }

        function handleDisabledCheckModalMouseDown(event) {
            if (event?.target !== event?.currentTarget) return;
            if (disabledCheckTaskLocked) return;
            hideDisabledCheckModal();
        }

        function setDisabledCheckModalMode(mode = 'batch') {
            const isGroupMode = mode === 'group';
            const titleEl = document.getElementById('disabledCheckModalTitle');
            const fileGroup = document.getElementById('disabledCheckFileInput')?.closest('.form-group');
            const inputGroup = document.getElementById('disabledCheckInput')?.closest('.form-group');
            const runBtn = document.getElementById('disabledCheckRunBtn');

            if (titleEl) {
                titleEl.textContent = isGroupMode ? '当前分组停用检测' : '批量停用检测';
            }
            if (fileGroup) {
                fileGroup.hidden = isGroupMode;
            }
            if (inputGroup) {
                inputGroup.hidden = isGroupMode;
            }
            if (runBtn) {
                runBtn.hidden = isGroupMode;
            }
        }

        function showDisabledCheckModal() {
            setDisabledCheckTaskLocked(false);
            setDisabledCheckModalMode('batch');
            showModal('disabledCheckModal');
            resetDisabledCheckResults();
            document.getElementById('disabledCheckInput')?.focus();
        }

        function hideDisabledCheckModal() {
            setDisabledCheckTaskLocked(false);
            hideModal('disabledCheckModal');
        }

        function getDisabledCheckInputLines() {
            const text = document.getElementById('disabledCheckInput')?.value || '';
            return text
                .split(/\r?\n/)
                .map(line => line.trim())
                .filter(Boolean);
        }

        function setDisabledCheckInputLines(lines) {
            const input = document.getElementById('disabledCheckInput');
            if (!input) return;
            const normalizedLines = Array.from(new Set(
                (lines || []).map(line => String(line || '').trim()).filter(Boolean)
            ));
            input.value = normalizedLines.join('\n');
        }

        function handleDisabledCheckFileSelect(event) {
            const file = event?.target?.files?.[0];
            if (!file) return;
            if (!/\.txt$/i.test(file.name) && file.type && file.type !== 'text/plain') {
                showToast('请选择 TXT 文档文件', 'error');
                event.target.value = '';
                return;
            }

            const reader = new FileReader();
            reader.onload = () => {
                const currentLines = getDisabledCheckInputLines();
                const fileLines = String(reader.result || '')
                    .split(/\r?\n/)
                    .map(line => line.trim())
                    .filter(Boolean);
                setDisabledCheckInputLines(currentLines.concat(fileLines));
                resetDisabledCheckResults();
                showToast(`已载入 ${fileLines.length} 行邮箱`, 'success');
            };
            reader.onerror = () => {
                showToast('读取 TXT 文件失败', 'error');
            };
            reader.readAsText(file, 'utf-8');
        }

        function formatDisabledCheckRate(value) {
            const numberValue = Number(value);
            if (!Number.isFinite(numberValue)) {
                return '0%';
            }
            return `${numberValue.toFixed(numberValue % 1 === 0 ? 0 : 2)}%`;
        }

        function renderDisabledCheckSummary(summary) {
            const summaryEl = document.getElementById('disabledCheckSummary');
            if (!summaryEl) return;
            const safeSummary = summary || {};
            summaryEl.innerHTML = `
                <div class="disabled-check-summary-card">
                    <div class="disabled-check-summary-card__label">输入账号</div>
                    <div class="disabled-check-summary-card__value">${Number(safeSummary.input_count || 0)}</div>
                </div>
                <div class="disabled-check-summary-card">
                    <div class="disabled-check-summary-card__label">已检测</div>
                    <div class="disabled-check-summary-card__value">${Number(safeSummary.checked_count || 0)}</div>
                </div>
                <div class="disabled-check-summary-card">
                    <div class="disabled-check-summary-card__label">停用账号</div>
                    <div class="disabled-check-summary-card__value danger">${Number(safeSummary.disabled_count || 0)}</div>
                </div>
                <div class="disabled-check-summary-card">
                    <div class="disabled-check-summary-card__label">停用占比</div>
                    <div class="disabled-check-summary-card__value danger">${formatDisabledCheckRate(safeSummary.disabled_rate || 0)}</div>
                </div>
                <div class="disabled-check-summary-card">
                    <div class="disabled-check-summary-card__label">本次标注</div>
                    <div class="disabled-check-summary-card__value danger">${Number(safeSummary.marked_inactive_count || 0)}</div>
                </div>
            `;
            summaryEl.hidden = false;
        }

        function renderDisabledCheckRunningState(message = '当前分组停用检测仍在执行，请稍候...') {
            const summaryEl = document.getElementById('disabledCheckSummary');
            const resultsEl = document.getElementById('disabledCheckResults');
            if (summaryEl) {
                summaryEl.innerHTML = `
                    <div class="disabled-check-loading-card">
                        <div class="disabled-check-spinner" aria-hidden="true"></div>
                        <div class="disabled-check-loading-text">
                            <strong>${escapeHtml(message)}</strong>
                            <span>正在并发登录账号并读取最近邮件，完成后会自动刷新结果。</span>
                        </div>
                    </div>
                `;
                summaryEl.hidden = false;
            }
            if (resultsEl) {
                resultsEl.hidden = true;
                resultsEl.innerHTML = '';
            }
        }

        function getDisabledCheckSubjects(row) {
            const matched = Array.isArray(row.matched_emails) ? row.matched_emails : [];
            const checked = Array.isArray(row.checked_emails) ? row.checked_emails : [];
            const source = matched.length ? matched : checked;
            if (!source.length) {
                return row.error || '-';
            }
            return source
                .map(item => {
                    const folder = item.folder ? `[${item.folder}] ` : '';
                    return `${folder}${item.subject || '无主题'}`;
                })
                .join('\n');
        }

        function renderDisabledCheckResults(results) {
            const resultsEl = document.getElementById('disabledCheckResults');
            if (!resultsEl) return;
            const rows = Array.isArray(results) ? results : [];
            if (!rows.length) {
                resultsEl.hidden = true;
                resultsEl.innerHTML = '';
                return;
            }

            resultsEl.innerHTML = `
                <table class="disabled-check-table">
                    <thead>
                        <tr>
                            <th>行</th>
                            <th>邮箱账号</th>
                            <th>结果</th>
                            <th>系统状态</th>
                            <th>标注</th>
                            <th>检查邮件</th>
                            <th>命中/最近主题</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(row => `
                            <tr>
                                <td>${escapeHtml(String(row.line || ''))}</td>
                                <td class="disabled-check-email-cell">${escapeHtml(row.email || '')}</td>
                                <td><span class="disabled-check-status ${escapeHtml(row.status || '')}">${escapeHtml(row.status_label || row.status || '-')}</span></td>
                                <td>${escapeHtml(row.account_status_after || row.account_status || '-')}</td>
                                <td>${row.marked_inactive ? '<span class="disabled-check-status disabled">已标注</span>' : '-'}</td>
                                <td>${Number(row.checked_count || 0)}</td>
                                <td class="disabled-check-subjects">${escapeHtml(getDisabledCheckSubjects(row))}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
            resultsEl.hidden = false;
        }

        async function runDisabledAccountCheck() {
            const btn = document.getElementById('disabledCheckRunBtn');
            if (!btn || btn.disabled) return;

            const emails = getDisabledCheckInputLines();
            if (!emails.length) {
                showToast('请输入或上传要检测的邮箱账号', 'error');
                return;
            }

            const recentCount = parseInt(document.getElementById('disabledCheckRecentCount')?.value || '10', 10);
            btn.disabled = true;
            btn.textContent = '检测中...';
            resetDisabledCheckResults();

            try {
                const response = await fetch('/api/accounts/disabled-check', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        emails,
                        recent_count: Number.isFinite(recentCount) ? recentCount : 10
                    })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, '批量停用检测失败');
                    return;
                }

                renderDisabledCheckSummary(data.summary);
                renderDisabledCheckResults(data.results);
                showToast(
                    `检测完成：停用 ${data.summary?.disabled_count || 0} / 已检测 ${data.summary?.checked_count || 0}`,
                    data.summary?.disabled_count > 0 ? 'warning' : 'success'
                );
            } catch (error) {
                showToast('批量停用检测失败', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = '检测';
            }
        }

        async function runCurrentGroupDisabledCheck() {
            const btn = document.getElementById('groupDisabledCheckBtn');
            if (!currentGroupId || isTempEmailGroup) {
                showToast('请选择普通邮箱分组', 'error');
                return;
            }
            if (btn?.disabled) return;

            const recentCount = parseInt(document.getElementById('disabledCheckRecentCount')?.value || '10', 10);
            if (btn) {
                btn.disabled = true;
                btn.dataset.loading = 'true';
            }
            setDisabledCheckModalMode('group');
            showModal('disabledCheckModal');
            setDisabledCheckTaskLocked(true);
            resetDisabledCheckResults();
            renderDisabledCheckRunningState('当前分组停用检测任务启动中...');

            try {
                const response = await fetch(`/api/groups/${encodeURIComponent(currentGroupId)}/accounts/disabled-check`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        recent_count: Number.isFinite(recentCount) ? recentCount : 10
                    })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, '当前分组停用检测失败');
                    return;
                }
                if (response.status === 202 || data.status === 'running') {
                    renderDisabledCheckRunningState(data.message || '当前分组停用检测正在后台执行...');
                    await pollCurrentGroupDisabledCheckTask(data.task_id);
                    return;
                }

                renderDisabledCheckSummary(data.summary);
                renderDisabledCheckResults(data.results);
                invalidateAccountCaches();
                await refreshVisibleAccountList(true);
                showToast(
                    data.message || `当前分组检测完成：本次标注 ${data.summary?.marked_inactive_count || 0} 个`,
                    data.summary?.disabled_count > 0 ? 'warning' : 'success'
                );
            } catch (error) {
                showToast('当前分组停用检测失败', 'error');
            } finally {
                setDisabledCheckTaskLocked(false);
                if (btn) {
                    btn.disabled = false;
                    btn.dataset.loading = 'false';
                }
            }
        }

        async function pollCurrentGroupDisabledCheckTask(taskId) {
            if (!taskId) {
                showToast('当前分组停用检测任务缺少任务 ID', 'error');
                return;
            }

            const maxAttempts = 240;
            for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
                await new Promise(resolve => setTimeout(resolve, 1000));
                renderDisabledCheckRunningState(`当前分组停用检测执行中... ${attempt}s`);

                const response = await fetch(`/api/groups/disabled-check-tasks/${encodeURIComponent(taskId)}`, {
                    method: 'GET',
                    cache: 'no-store'
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, '当前分组停用检测失败');
                    return;
                }
                if (data.task_status === 'running' || data.status === 'running') {
                    continue;
                }

                renderDisabledCheckSummary(data.summary);
                renderDisabledCheckResults(data.results);
                invalidateAccountCaches();
                await refreshVisibleAccountList(true);
                showToast(
                    data.message || `当前分组检测完成：本次标注 ${data.summary?.marked_inactive_count || 0} 个`,
                    data.summary?.disabled_count > 0 ? 'warning' : 'success'
                );
                return;
            }

            showToast('当前分组停用检测仍在执行，请稍后刷新结果', 'warning');
        }

        function formatDisabledCheckTaskTime(value) {
            if (!value) return '-';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) {
                return String(value);
            }
            return date.toLocaleString();
        }

        function getDisabledCheckTaskStatusLabel(status) {
            const normalized = String(status || '').toLowerCase();
            if (normalized === 'completed') return '已完成';
            if (normalized === 'failed') return '失败';
            if (normalized === 'running') return '执行中';
            return normalized || '-';
        }

        function renderDisabledCheckHistory(tasks) {
            const listEl = document.getElementById('disabledCheckHistoryList');
            if (!listEl) return;
            const rows = Array.isArray(tasks) ? tasks : [];
            if (!rows.length) {
                listEl.innerHTML = '<div class="disabled-check-history-empty">暂无停用检测任务历史</div>';
                return;
            }

            listEl.innerHTML = `
                <table class="disabled-check-history-table">
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>分组</th>
                            <th>状态</th>
                            <th>已检测</th>
                            <th>停用</th>
                            <th>已标注</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.map(task => `
                            <tr>
                                <td>${escapeHtml(formatDisabledCheckTaskTime(task.created_at))}</td>
                                <td>${escapeHtml(task.group_name || `分组 ${task.group_id || '-'}`)}</td>
                                <td><span class="disabled-check-status ${escapeHtml(task.status || '')}">${escapeHtml(getDisabledCheckTaskStatusLabel(task.status || task.task_status))}</span></td>
                                <td>${Number(task.checked_count || task.summary?.checked_count || 0)}</td>
                                <td>${Number(task.disabled_count || task.summary?.disabled_count || 0)}</td>
                                <td>${Number(task.marked_inactive_count || task.summary?.marked_inactive_count || 0)}</td>
                                <td>
                                    <button class="btn btn-secondary btn-sm" onclick="viewDisabledCheckHistoryTask('${escapeHtml(task.task_id || '')}')">查看</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        async function loadDisabledCheckHistory() {
            const listEl = document.getElementById('disabledCheckHistoryList');
            if (listEl) {
                listEl.innerHTML = '<div class="disabled-check-history-empty">正在加载任务历史...</div>';
            }
            const params = new URLSearchParams({ limit: '50' });
            if (currentGroupId && !isTempEmailGroup) {
                params.set('group_id', String(currentGroupId));
            }
            try {
                const response = await fetch(`/api/groups/disabled-check-tasks/history?${params.toString()}`, {
                    method: 'GET',
                    cache: 'no-store'
                });
                const data = await response.json();
                if (!data.success) {
                    handleApiError(data, '加载停用检测任务历史失败');
                    return;
                }
                renderDisabledCheckHistory(data.tasks);
            } catch (error) {
                showToast('加载停用检测任务历史失败', 'error');
            }
        }

        function showDisabledCheckHistoryModal() {
            showModal('disabledCheckHistoryModal');
            loadDisabledCheckHistory();
        }

        function hideDisabledCheckHistoryModal() {
            hideModal('disabledCheckHistoryModal');
        }

        async function viewDisabledCheckHistoryTask(taskId) {
            if (!taskId) return;
            try {
                const response = await fetch(`/api/groups/disabled-check-tasks/${encodeURIComponent(taskId)}`, {
                    method: 'GET',
                    cache: 'no-store'
                });
                const data = await response.json();
                if (!data.success) {
                    handleApiError(data, '加载停用检测任务详情失败');
                    return;
                }
                hideDisabledCheckHistoryModal();
                setDisabledCheckTaskLocked(false);
                setDisabledCheckModalMode('group');
                showModal('disabledCheckModal');
                resetDisabledCheckResults();
                if (data.task_status === 'running' || data.status === 'running') {
                    renderDisabledCheckRunningState(data.message || '当前分组停用检测仍在执行');
                    return;
                }
                renderDisabledCheckSummary(data.summary);
                renderDisabledCheckResults(data.results);
            } catch (error) {
                showToast('加载停用检测任务详情失败', 'error');
            }
        }

        async function deleteSelectedAccounts() {
            const btn = document.getElementById('batchDeleteAccountsBtn');
            if (!btn || btn.disabled) return;

            const checked = Array.from(document.querySelectorAll('#accountList .account-select-checkbox:checked'));
            const accountIds = checked
                .map(cb => parseInt(cb.value, 10))
                .filter(Number.isFinite);
            const accountEmails = checked
                .map(cb => cb.dataset.accountEmail || '')
                .filter(Boolean);
            const isTempContext = !!isTempEmailGroup;

            if (!accountIds.length) {
                showToast(isTempContext ? '请先选择要删除的临时邮箱' : '请先选择要删除的邮箱', 'error');
                return;
            }

            const resourceLabel = isTempContext ? '临时邮箱' : '邮箱';
            if (!(await showConfirmModal(`确定要删除所选 ${accountIds.length} 个${resourceLabel}吗？此操作不可恢复。`, { title: `批量删除${resourceLabel}`, confirmText: '确认删除' }))) {
                return;
            }

            btn.disabled = true;
            btn.dataset.loading = 'true';
            btn.textContent = '删除中...';

            try {
                const response = await fetch(isTempContext ? '/api/temp-emails/batch-delete' : '/api/accounts/batch-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(isTempContext
                        ? { temp_email_ids: accountIds }
                        : { account_ids: accountIds })
                });
                const data = await response.json();

                if (!data.success) {
                    handleApiError(data, '批量删除失败');
                    return;
                }

                const deletedEmails = Array.isArray(isTempContext ? data.deleted_emails : data.deleted_accounts)
                    ? (isTempContext ? data.deleted_emails : data.deleted_accounts).map(item => item.email).filter(Boolean)
                    : accountEmails;

                showToast(data.message || `已删除 ${deletedEmails.length} 个${resourceLabel}`, 'success');
                if (isTempContext) {
                    delete accountsCache.temp;
                } else {
                    invalidateAccountCaches();
                }
                resetSelectedAccountViewIfDeleted(deletedEmails);
                clearAccountSelection();
                loadGroups();
                await refreshVisibleAccountList(true);
            } catch (error) {
                showToast('批量删除失败', 'error');
            } finally {
                btn.dataset.loading = 'false';
                updateBatchActionBar();
            }
        }

        let batchActionType = ''; // 'add' or 'remove'

        // 显示批量打标模态框
        async function showBatchTagModal(type) {
            batchActionType = type;
            const resourceLabel = isTempEmailGroup ? '临时邮箱' : '账号';
            document.getElementById('batchTagTitle').textContent = type === 'add'
                ? `批量给${resourceLabel}添加标签`
                : `批量移除${resourceLabel}标签`;
            showModal('batchTagModal');

            // 加载标签选项
            await loadTagsForSelect();
        }

        function hideBatchTagModal() {
            hideModal('batchTagModal');
        }

        // 加载标签到下拉框
        async function loadTagsForSelect() {
            const select = document.getElementById('batchTagSelect');
            select.innerHTML = '<option value="">加载中...</option>';

            try {
                const response = await fetch('/api/tags');
                const data = await response.json();
                if (data.success) {
                    let html = '<option value="">请选择标签...</option>';
                    data.tags.forEach(tag => {
                        html += `<option value="${tag.id}">${escapeHtml(tag.name)}</option>`;
                    });
                    select.innerHTML = html;
                }
            } catch (error) {
                select.innerHTML = '<option value="">加载失败</option>';
            }
        }

        // 确认批量打标
        async function confirmBatchTag() {
            const tagId = document.getElementById('batchTagSelect').value;
            if (!tagId) {
                showToast('请选择标签', 'error');
                return;
            }

            const checked = document.querySelectorAll('.account-select-checkbox:checked');
            const accountIds = Array.from(checked).map(cb => parseInt(cb.value));

            if (accountIds.length === 0) return;

            try {
                const isTempContext = !!isTempEmailGroup;
                const response = await fetch(isTempContext ? '/api/temp-emails/tags' : '/api/accounts/tags', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(isTempContext
                        ? {
                            temp_email_ids: accountIds,
                            tag_id: parseInt(tagId, 10),
                            action: batchActionType
                        }
                        : {
                            account_ids: accountIds,
                            tag_id: parseInt(tagId, 10),
                            action: batchActionType
                        })
                });

                const data = await response.json();
                if (data.success) {
                    showToast(data.message, 'success');
                    hideBatchTagModal();
                    await refreshVisibleAccountList(true);
                    // 隐藏操作栏
                    document.querySelectorAll('.account-select-checkbox').forEach(cb => cb.checked = false);
                    updateBatchActionBar();
                } else {
                    showToast(data.error || '操作失败', 'error');
                }
            } catch (error) {
                showToast('请求失败', 'error');
            }
        }

        // ==================== 批量移动分组 ====================

        // 显示批量移动分组模态框
        async function showBatchMoveGroupModal() {
            showModal('batchMoveGroupModal');
            await loadGroupsForBatchMove();
        }

        function hideBatchMoveGroupModal() {
            hideModal('batchMoveGroupModal');
        }

        // 加载分组到下拉框
        async function loadGroupsForBatchMove() {
            const select = document.getElementById('batchMoveGroupSelect');
            select.innerHTML = '<option value="">加载中...</option>';

            try {
                const response = await fetch('/api/groups');
                const data = await response.json();
                if (data.success) {
                    let html = '<option value="">请选择分组...</option>';
                    data.groups.filter(g => !g.is_system).forEach(group => {
                        html += `<option value="${group.id}">${escapeHtml(normalizeGroupName(group.name))}</option>`;
                    });
                    select.innerHTML = html;
                }
            } catch (error) {
                select.innerHTML = '<option value="">加载失败</option>';
            }
        }

        // 确认批量移动分组
        async function confirmBatchMoveGroup() {
            const groupId = document.getElementById('batchMoveGroupSelect').value;
            if (!groupId) {
                showToast('请选择目标分组', 'error');
                return;
            }

            const checked = document.querySelectorAll('.account-select-checkbox:checked');
            const accountIds = Array.from(checked).map(cb => parseInt(cb.value));

            if (accountIds.length === 0) return;

            try {
                const response = await fetch('/api/accounts/batch-update-group', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        account_ids: accountIds,
                        group_id: parseInt(groupId)
                    })
                });

                const data = await response.json();
                if (data.success) {
                    showToast(data.message, 'success');
                    hideBatchMoveGroupModal();
                    // 刷新分组列表
                    loadGroups();
                    invalidateAccountCaches();
                    await refreshVisibleAccountList(true);
                    // 清除选择
                    document.querySelectorAll('.account-select-checkbox').forEach(cb => cb.checked = false);
                    updateBatchActionBar();
                } else {
                    showToast(data.error || '操作失败', 'error');
                }
            } catch (error) {
                showToast('请求失败', 'error');
            }
        }
