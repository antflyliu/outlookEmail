from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    # These segmented files are executed into the shared `web_outlook_app`
    # globals at runtime. Importing from the assembled module keeps IDE
    # inspections from flagging the shared names as unresolved.
    from web_outlook_app import *  # noqa: F403


TOKEN_REFRESH_SCOPE_KEY = 'all_outlook'
VALID_ACCOUNT_REFRESH_STATUSES = {'success', 'failed', 'never'}
VALID_REFRESH_STATUS_FILTERS = {'all', 'success', 'failed', 'never'}
TOKEN_REFRESH_CONFLICT_MESSAGE = '已有 Token 全量刷新任务在执行，请稍后再试'
TOKEN_REFRESH_STOP_REQUESTED_MESSAGE = '已请求停止，当前账号处理完成后会结束任务'
TOKEN_REFRESH_STOPPED_MESSAGE = '已手动停止全量刷新任务'
SELECTED_REFRESH_TASK_TTL_SECONDS = 300
LOG_PAGINATION_DEFAULT_LIMIT = 100
LOG_PAGINATION_MAX_LIMIT = 1000
token_refresh_stop_event = threading.Event()
selected_refresh_tasks: Dict[str, Dict[str, Any]] = {}
selected_refresh_tasks_lock = threading.Lock()


def normalize_account_refresh_status_value(status: Any) -> str:
    normalized = str(status or '').strip().lower()
    if normalized in VALID_ACCOUNT_REFRESH_STATUSES:
        return normalized
    return 'never'


def normalize_refresh_status_filter(status: Any) -> str:
    normalized = str(status or '').strip().lower()
    if normalized in VALID_REFRESH_STATUS_FILTERS:
        return normalized
    return 'all'


def get_account_field(account: Any, field_name: str, default: Any) -> Any:
    if account is None:
        return default
    if isinstance(account, dict):
        return account.get(field_name, default)
    try:
        return account[field_name]
    except (KeyError, IndexError, TypeError, AttributeError):
        return default


def parse_log_pagination(limit_value: Any = None, offset_value: Any = 0,
                         default_limit: int = LOG_PAGINATION_DEFAULT_LIMIT,
                         max_limit: int = LOG_PAGINATION_MAX_LIMIT) -> tuple[int, int]:
    try:
        limit = int(limit_value) if limit_value not in (None, '') else default_limit
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(offset_value or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)


def escape_sql_like_literal(value: Any, escape_char: str = '\\') -> str:
    text = str(value or '')
    return (
        text
        .replace(escape_char, escape_char + escape_char)
        .replace('%', escape_char + '%')
        .replace('_', escape_char + '_')
    )


def is_outlook_refreshable_account(account: Any) -> bool:
    if account is None:
        return False
    account_type = str(get_account_field(account, 'account_type', 'outlook') or 'outlook').strip().lower()
    status = str(get_account_field(account, 'status', 'active') or 'active').strip().lower()
    return account_type == 'outlook' and status == 'active'


def ensure_token_refresh_state_row(db_conn=None) -> None:
    db = db_conn or get_db()
    db.execute(
        '''
        INSERT OR IGNORE INTO token_refresh_state (
            scope_key, trigger_type, status, total_count, success_count, failed_count, updated_at
        )
        VALUES (?, ?, ?, 0, 0, 0, CURRENT_TIMESTAMP)
        ''',
        (TOKEN_REFRESH_SCOPE_KEY, '', 'idle')
    )


def get_token_refresh_snapshot(db_conn=None) -> Dict[str, Any]:
    db = db_conn or get_db()
    ensure_token_refresh_state_row(db)
    row = db.execute(
        '''
        SELECT *
        FROM token_refresh_state
        WHERE scope_key = ?
        LIMIT 1
        ''',
        (TOKEN_REFRESH_SCOPE_KEY,)
    ).fetchone()
    return dict(row) if row else {}


def mark_token_refresh_snapshot_running(trigger_type: str, total_count: int, db_conn=None) -> None:
    db = db_conn or get_db()
    ensure_token_refresh_state_row(db)
    db.execute(
        '''
        UPDATE token_refresh_state
        SET trigger_type = ?,
            status = 'running',
            started_at = CURRENT_TIMESTAMP,
            finished_at = NULL,
            total_count = ?,
            success_count = 0,
            failed_count = 0,
            error_summary = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE scope_key = ?
        ''',
        (trigger_type, max(0, int(total_count or 0)), TOKEN_REFRESH_SCOPE_KEY)
    )


def mark_token_refresh_snapshot_finished(trigger_type: str, total_count: int,
                                         success_count: int, failed_count: int,
                                         error_summary: str = '', db_conn=None,
                                         status_override: Optional[str] = None) -> None:
    db = db_conn or get_db()
    ensure_token_refresh_state_row(db)
    normalized_total = max(0, int(total_count or 0))
    normalized_success = max(0, int(success_count or 0))
    normalized_failed = max(0, int(failed_count or 0))
    if status_override in {'idle', 'success', 'partial_failed', 'failed'}:
        final_status = status_override
    elif normalized_total <= 0:
        final_status = 'idle'
    elif normalized_failed <= 0:
        final_status = 'success'
    elif normalized_success > 0:
        final_status = 'partial_failed'
    else:
        final_status = 'failed'

    db.execute(
        '''
        UPDATE token_refresh_state
        SET trigger_type = ?,
            status = ?,
            finished_at = CURRENT_TIMESTAMP,
            total_count = ?,
            success_count = ?,
            failed_count = ?,
            error_summary = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE scope_key = ?
        ''',
        (
            trigger_type,
            final_status,
            normalized_total,
            normalized_success,
            normalized_failed,
            sanitize_error_details(error_summary)[:500] if error_summary else None,
            TOKEN_REFRESH_SCOPE_KEY,
        )
    )


def query_refreshable_accounts(db_conn=None, account_ids: Optional[List[int]] = None,
                               search: str = '', refresh_status: str = 'all',
                               page: int = 1, page_size: int = 100) -> Dict[str, Any]:
    db = db_conn or get_db()
    refresh_status = normalize_refresh_status_filter(refresh_status)
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 100)))
    offset = (page - 1) * page_size

    where_clauses = [
        "COALESCE(a.account_type, 'outlook') = 'outlook'",
        "a.status = 'active'",
    ]
    params: List[Any] = []

    normalized_search = str(search or '').strip()
    if normalized_search:
        where_clauses.append(
            "(a.email LIKE ? ESCAPE '\\' OR COALESCE(a.remark, '') LIKE ? ESCAPE '\\' OR COALESCE(g.name, '') LIKE ? ESCAPE '\\')"
        )
        like_value = f'%{escape_sql_like_literal(normalized_search)}%'
        params.extend([like_value, like_value, like_value])


    if refresh_status != 'all':
        where_clauses.append("COALESCE(NULLIF(a.last_refresh_status, ''), 'never') = ?")
        params.append(refresh_status)

    if account_ids is not None:
        normalized_ids = []
        seen_ids = set()
        for account_id in account_ids:
            try:
                normalized_id = int(account_id)
            except (TypeError, ValueError):
                continue
            if normalized_id <= 0 or normalized_id in seen_ids:
                continue
            seen_ids.add(normalized_id)
            normalized_ids.append(normalized_id)
        if not normalized_ids:
            return {'items': [], 'total': 0, 'page': page, 'page_size': page_size}
        where_clauses.append(f"a.id IN ({','.join('?' * len(normalized_ids))})")
        params.extend(normalized_ids)

    where_sql = ' AND '.join(where_clauses)
    total_row = db.execute(
        f'''
        SELECT COUNT(*) AS total_count
        FROM accounts a
        LEFT JOIN groups g ON a.group_id = g.id
        WHERE {where_sql}
        ''',
        tuple(params)
    ).fetchone()
    total_count = total_row['total_count'] if total_row else 0

    rows = db.execute(
        f'''
        SELECT a.*, g.name AS group_name, g.color AS group_color
        FROM accounts a
        LEFT JOIN groups g ON a.group_id = g.id
        WHERE {where_sql}
        ORDER BY
            CASE COALESCE(NULLIF(a.last_refresh_status, ''), 'never')
                WHEN 'failed' THEN 0
                WHEN 'never' THEN 1
                ELSE 2
            END,
            CASE WHEN a.last_refresh_at IS NULL OR a.last_refresh_at = '' THEN 1 ELSE 0 END,
            a.last_refresh_at DESC,
            a.created_at DESC,
            a.id DESC
        LIMIT ? OFFSET ?
        ''',
        tuple([*params, page_size, offset])
    ).fetchall()

    accounts = [resolve_account_record(row) for row in rows]
    tags_by_account = get_account_tags_map([account['id'] for account in accounts], db)
    items = []
    for account in accounts:
        account['tags'] = tags_by_account.get(account['id'], [])
        items.append(serialize_account_summary(account))

    return {
        'items': items,
        'total': total_count,
        'page': page,
        'page_size': page_size,
    }


def build_refresh_stats(db_conn=None) -> Dict[str, Any]:
    db = db_conn or get_db()
    ensure_token_refresh_state_row(db)
    snapshot = get_token_refresh_snapshot(db)

    counts_row = db.execute(
        '''
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN COALESCE(NULLIF(last_refresh_status, ''), 'never') = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN COALESCE(NULLIF(last_refresh_status, ''), 'never') = 'failed' THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN COALESCE(NULLIF(last_refresh_status, ''), 'never') = 'never' THEN 1 ELSE 0 END) AS never_count
        FROM accounts
        WHERE status = 'active'
          AND COALESCE(account_type, 'outlook') = 'outlook'
        '''
    ).fetchone()

    total_count = counts_row['total_count'] if counts_row and counts_row['total_count'] is not None else 0
    success_count = counts_row['success_count'] if counts_row and counts_row['success_count'] is not None else 0
    failed_count = counts_row['failed_count'] if counts_row and counts_row['failed_count'] is not None else 0
    never_count = counts_row['never_count'] if counts_row and counts_row['never_count'] is not None else 0

    snapshot_status = str(snapshot.get('status') or '').strip().lower()
    last_refresh_time = snapshot.get('finished_at')
    if not last_refresh_time:
        row = db.execute(
            '''
            SELECT MAX(created_at) AS last_refresh_time
            FROM account_refresh_logs
            WHERE refresh_type IN ('manual', 'scheduled')
            '''
        ).fetchone()
        last_refresh_time = row['last_refresh_time'] if row else None

    if snapshot_status not in {'running', 'success', 'partial_failed', 'failed'}:
        if total_count <= 0 or never_count == total_count:
            snapshot_status = 'idle'
        elif failed_count <= 0:
            snapshot_status = 'success'
        elif success_count > 0:
            snapshot_status = 'partial_failed'
        else:
            snapshot_status = 'failed'

    return {
        'total': total_count,
        'success_count': success_count,
        'failed_count': failed_count,
        'never_count': never_count,
        'last_refresh_time': last_refresh_time,
        'last_refresh_status': snapshot_status,
        'running': snapshot_status == 'running',
        'trigger_type': snapshot.get('trigger_type') or '',
        'error_summary': snapshot.get('error_summary') or '',
    }


class TokenRefreshInProgressError(RuntimeError):
    """当前已有全量刷新任务执行中。"""


def cleanup_refresh_logs(db_conn=None) -> int:
    db = db_conn or get_db()
    should_commit = db_conn is None
    try:
        cursor = db.execute(
            "DELETE FROM account_refresh_logs WHERE created_at < datetime('now', '-6 months')"
        )
        if should_commit:
            db.commit()
        return max(0, int(cursor.rowcount or 0))
    except Exception:
        if should_commit:
            try:
                db.rollback()
            except Exception:
                pass
        raise


def clear_token_refresh_stop_request() -> None:
    token_refresh_stop_event.clear()


def request_token_refresh_stop() -> None:
    token_refresh_stop_event.set()


def is_token_refresh_stop_requested() -> bool:
    return token_refresh_stop_event.is_set()


def acquire_token_refresh_run_lock() -> None:
    if not token_refresh_run_lock.acquire(blocking=False):
        raise TokenRefreshInProgressError(TOKEN_REFRESH_CONFLICT_MESSAGE)


def release_token_refresh_run_lock(acquired: bool) -> None:
    if not acquired:
        return
    try:
        token_refresh_run_lock.release()
    except RuntimeError:
        pass


def build_refresh_error_summary(failed_list: List[Dict[str, Any]], fallback_message: str = '') -> str:
    parts = [
        f"{item.get('email') or 'unknown'}: {item.get('error') or '未知错误'}"
        for item in failed_list[:5]
    ]
    if not parts and fallback_message:
        parts.append(sanitize_error_details(fallback_message))
    return '; '.join(parts)


def finalize_stopped_full_refresh(conn, snapshot_trigger_type: str, total: int,
                                  success_count: int, failed_count: int,
                                  failed_list: List[Dict[str, Any]],
                                  delay_seconds: int = 0) -> Dict[str, Any]:
    processed_count = max(0, success_count + failed_count)
    if total <= 0:
        final_status = 'idle'
    elif processed_count <= 0:
        final_status = 'failed'
    elif processed_count < total:
        final_status = 'partial_failed' if success_count > 0 else 'failed'
    elif failed_count <= 0:
        final_status = 'success'
    elif success_count > 0:
        final_status = 'partial_failed'
    else:
        final_status = 'failed'

    error_summary = build_refresh_error_summary(failed_list, TOKEN_REFRESH_STOPPED_MESSAGE)
    mark_token_refresh_snapshot_finished(
        snapshot_trigger_type,
        total,
        success_count,
        failed_count,
        error_summary,
        conn,
        status_override=final_status
    )
    conn.commit()

    return build_stopped_refresh_payload(
        total,
        success_count,
        failed_count,
        failed_list,
        delay_seconds=delay_seconds,
        refresh_type=snapshot_trigger_type,
    )


def build_stopped_refresh_payload(total: int, success_count: int, failed_count: int,
                                  failed_list: List[Dict[str, Any]],
                                  delay_seconds: int = 0,
                                  refresh_type: str = '') -> Dict[str, Any]:
    processed_count = max(0, success_count + failed_count)
    return {
        'type': 'stopped',
        'message': TOKEN_REFRESH_STOPPED_MESSAGE,
        'total': total,
        'processed_count': processed_count,
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list,
        'delay_seconds': max(0, int(delay_seconds or 0)),
        'refresh_type': refresh_type,
    }


def wait_refresh_delay(delay_seconds: int) -> bool:
    import time as time_module

    remaining = max(0.0, float(delay_seconds or 0))
    while remaining > 0:
        if is_token_refresh_stop_requested():
            return False
        sleep_seconds = min(0.25, remaining)
        time_module.sleep(sleep_seconds)
        remaining -= sleep_seconds
    return not is_token_refresh_stop_requested()


def finalize_aborted_full_refresh(conn, snapshot_trigger_type: str, log_refresh_type: str,
                                  total: int, success_count: int, failed_count: int,
                                  failed_list: List[Dict[str, Any]], current_account=None,
                                  current_account_counted: bool = False,
                                  error: Exception | None = None) -> Dict[str, Any]:
    failure_message = sanitize_error_details(str(error or '未知错误')) or '未知错误'
    active_total = max(0, int(total or 0))

    if current_account is not None and not current_account_counted:
        failed_count += 1
        failed_item = {
            'id': current_account['id'],
            'email': current_account['email'],
            'error': failure_message,
        }
        failed_list.append(failed_item)
        try:
            log_refresh_result(
                current_account['id'],
                current_account['email'],
                log_refresh_type,
                'failed',
                failure_message,
                db_conn=conn
            )
            conn.commit()
        except Exception as log_error:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"记录异常刷新结果失败: {str(log_error)}")
        if active_total <= 0:
            active_total = success_count + failed_count

    final_status = 'partial_failed' if success_count > 0 else 'failed'
    error_summary = build_refresh_error_summary(failed_list, failure_message)
    mark_token_refresh_snapshot_finished(
        snapshot_trigger_type,
        active_total,
        success_count,
        failed_count,
        error_summary,
        conn,
        status_override=final_status
    )
    conn.commit()

    return {
        'type': 'error',
        'message': failure_message,
        'total': active_total,
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list,
        'refresh_type': snapshot_trigger_type,
    }


def finalize_aborted_retry_refresh(conn, log_refresh_type: str,
                                   total: int, success_count: int, failed_count: int,
                                   failed_list: List[Dict[str, Any]], current_account=None,
                                   current_account_counted: bool = False,
                                   error: Exception | None = None) -> Dict[str, Any]:
    failure_message = sanitize_error_details(str(error or '未知错误')) or '未知错误'
    active_total = max(0, int(total or 0))

    if current_account is not None and not current_account_counted:
        failed_count += 1
        failed_item = {
            'id': current_account['id'],
            'email': current_account['email'],
            'error': failure_message,
        }
        failed_list.append(failed_item)
        try:
            log_refresh_result(
                current_account['id'],
                current_account['email'],
                log_refresh_type,
                'failed',
                failure_message,
                db_conn=conn
            )
            conn.commit()
        except Exception as log_error:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"记录异常重试结果失败: {str(log_error)}")
        if active_total <= 0:
            active_total = success_count + failed_count

    return {
        'type': 'error',
        'message': failure_message,
        'total': active_total,
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list,
        'refresh_type': 'retry_failed',
    }


def log_refresh_result(account_id: int, account_email: str, refresh_type: str, status: str,
                       error_message: str = None, db_conn=None):
    """记录刷新结果到数据库"""
    db = db_conn or get_db()
    should_commit = db_conn is None
    normalized_status = 'success' if str(status or '').strip().lower() == 'success' else 'failed'
    sanitized_error = sanitize_error_details(error_message)[:500] if error_message else None
    try:
        db.execute('''
            INSERT INTO account_refresh_logs (account_id, account_email, refresh_type, status, error_message)
            VALUES (?, ?, ?, ?, ?)
        ''', (account_id, account_email, refresh_type, normalized_status, sanitized_error))

        db.execute(
            '''
            UPDATE accounts
            SET last_refresh_at = CURRENT_TIMESTAMP,
                last_refresh_status = ?,
                last_refresh_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (
                normalized_status,
                sanitized_error if normalized_status == 'failed' else None,
                account_id,
            )
        )

        if should_commit:
            db.commit()
        return True
    except Exception as e:
        if should_commit:
            try:
                db.rollback()
            except Exception:
                pass
        print(f"记录刷新结果失败: {str(e)}")
        return False


def persist_rotated_refresh_token(account_id: int, refresh_token: str, db_conn=None) -> bool:
    """保存微软返回的新 refresh_token。"""
    token_value = str(refresh_token or '').strip()
    if not token_value:
        return False

    db = db_conn or get_db()
    should_commit = db_conn is None
    try:
        db.execute(
            '''
            UPDATE accounts
            SET refresh_token = ?, refresh_token_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (encrypt_data(token_value), account_id)
        )
        if should_commit:
            db.commit()
        return True
    except Exception as e:
        if should_commit:
            try:
                db.rollback()
            except Exception:
                pass
        print(f"保存轮换 refresh_token 失败: {str(e)}")
        return False


def extract_token_response_error(response, fallback: str = '未知错误') -> str:
    try:
        error_data = response.json()
    except Exception:
        return str(getattr(response, 'text', '') or getattr(response, 'reason', '') or fallback)
    return str(error_data.get('error_description', error_data.get('error', fallback)) or fallback)


def log_forwarding_result(account_id: int, account_email: str, message_id: str, channel: str,
                          status: str, error_message: str = None, db_conn=None):
    """记录转发结果到数据库"""
    db = db_conn or get_db()
    should_commit = db_conn is None
    try:
        db.execute('''
            INSERT INTO forwarding_logs (account_id, account_email, message_id, channel, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            account_id,
            account_email,
            str(message_id or ''),
            channel,
            status,
            sanitize_error_details(error_message)[:500] if error_message else None,
        ))
        if should_commit:
            db.commit()
        return True
    except Exception as e:
        if should_commit:
            try:
                db.rollback()
            except Exception:
                pass
        print(f"记录转发结果失败: {str(e)}")
        return False


def test_refresh_token(client_id: str, refresh_token: str, proxy_url: str = None,
                       fallback_proxy_urls: List[str] = None) -> tuple[bool, Optional[str], str]:
    """测试 refresh token 是否有效，返回 (是否成功, 错误信息, 新 refresh_token)"""
    try:
        graph_res = request_graph_token_response(
            client_id,
            refresh_token,
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
            include_original_scope_fallback=True,
        )
    except Exception as e:
        return False, f"Graph 刷新请求异常: {str(e)}", ''

    if graph_res.status_code == 200:
        payload = {}
        try:
            payload = graph_res.json()
        except Exception:
            payload = {}
        return True, None, str(payload.get('refresh_token') or '').strip()

    graph_error_msg = extract_token_response_error(graph_res)

    try:
        imap_res = request_imap_token_response(
            client_id,
            refresh_token,
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
        )
    except Exception as e:
        return False, f"Graph 刷新失败: {graph_error_msg}; IMAP 刷新请求异常: {str(e)}", ''

    if imap_res.status_code == 200:
        payload = {}
        try:
            payload = imap_res.json()
        except Exception:
            payload = {}
        return True, None, str(payload.get('refresh_token') or '').strip()

    imap_error_msg = extract_token_response_error(imap_res)
    return False, f"Graph 刷新失败: {graph_error_msg}; IMAP 刷新失败: {imap_error_msg}", ''


def refresh_outlook_account_token(account: sqlite3.Row, refresh_type: str = 'manual',
                                  db_conn=None) -> Dict[str, Any]:
    """刷新单个 Outlook 账号的 refresh token 并记录结果。"""
    account_id = account['id']
    account_email = account['email']
    client_id = account['client_id']
    encrypted_refresh_token = account['refresh_token']

    group_id = account['group_id']
    group_row = None
    if db_conn is not None and group_id:
        group_row = db_conn.execute(
            'SELECT proxy_url, fallback_proxy_url_1, fallback_proxy_url_2 FROM groups WHERE id = ?',
            (group_id,)
        ).fetchone()
    if group_row:
        proxy_url = get_group_proxy_url(dict(group_row))
        fallback_proxy_urls = get_group_proxy_failover_urls(dict(group_row))
    else:
        proxy_config = get_account_proxy_config(dict(account))
        proxy_url = proxy_config.get('proxy_url', '') or ''
        fallback_proxy_urls = get_account_proxy_failover_urls(dict(account))

    # 解密 refresh_token
    try:
        refresh_token = decrypt_data(encrypted_refresh_token) if encrypted_refresh_token else encrypted_refresh_token
    except Exception as e:
        error_msg = sanitize_error_details(f"解密 token 失败: {str(e)}")
        log_refresh_result(account_id, account_email, refresh_type, 'failed', error_msg, db_conn=db_conn)
        return {
            'success': False,
            'error_message': error_msg,
            'error_payload': build_error_payload(
                "TOKEN_DECRYPT_FAILED",
                "Token 解密失败",
                "DecryptionError",
                500,
                error_msg
            )
        }

    success, error_msg, rotated_refresh_token = test_refresh_token(
        client_id,
        refresh_token,
        proxy_url,
        fallback_proxy_urls,
    )
    sanitized_error = sanitize_error_details(error_msg) if error_msg else ''

    if success and rotated_refresh_token and rotated_refresh_token != refresh_token:
        persist_rotated_refresh_token(account_id, rotated_refresh_token, db_conn)

    # 记录刷新结果
    log_refresh_result(
        account_id,
        account_email,
        refresh_type,
        'success' if success else 'failed',
        sanitized_error or None,
        db_conn=db_conn
    )

    if success:
        return {'success': True, 'message': 'Token 刷新成功'}

    return {
        'success': False,
        'error_message': sanitized_error or '未知错误',
        'error_payload': build_error_payload(
            "TOKEN_REFRESH_FAILED",
            "Token 刷新失败",
            "RefreshTokenError",
            400,
            sanitized_error or "未知错误"
        )
    }


@app.route('/api/accounts/<int:account_id>/refresh', methods=['POST'])
@login_required
def api_refresh_account(account_id):
    """刷新单个账号的 token"""
    db = get_db()
    cursor = db.execute('SELECT id, email, client_id, refresh_token, group_id, account_type, provider FROM accounts WHERE id = ?', (account_id,))
    account = cursor.fetchone()

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "账号不存在",
            "NotFoundError",
            404,
            f"account_id={account_id}"
        )
        return jsonify({'success': False, 'error': error_payload})

    if (account['account_type'] or '').strip().lower() == 'imap':
        return jsonify({'success': False, 'error': 'IMAP 账号无需刷新 Token'})

    cleanup_refresh_logs()
    result = refresh_outlook_account_token(account, 'manual')
    if result['success']:
        return jsonify({'success': True, 'message': result['message']})
    return jsonify({'success': False, 'error': result['error_payload']})


@app.route('/api/accounts/refresh-selected', methods=['POST'])
@login_required
def api_refresh_selected_accounts():
    """刷新选中账号的 token。"""
    data = request.get_json(silent=True) or {}
    raw_account_ids = data.get('account_ids') or []

    if not isinstance(raw_account_ids, list):
        return jsonify({'success': False, 'error': '账号列表格式错误'})

    account_ids = []
    seen_ids = set()
    for account_id in raw_account_ids:
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            continue
        if normalized_id <= 0 or normalized_id in seen_ids:
            continue
        seen_ids.add(normalized_id)
        account_ids.append(normalized_id)

    if not account_ids:
        return jsonify({'success': False, 'error': '请选择要刷新的账号'})

    cleanup_refresh_logs()
    db = get_db()
    placeholders = ','.join('?' * len(account_ids))
    cursor = db.execute(f'''
        SELECT id, email, client_id, refresh_token, group_id, account_type, provider
        FROM accounts
        WHERE id IN ({placeholders})
        ORDER BY email COLLATE NOCASE ASC
    ''', account_ids)
    accounts = cursor.fetchall()

    found_ids = {row['id'] for row in accounts}
    success_count = 0
    failed_count = 0
    skipped_count = 0
    failed_list = []
    skipped_list = []

    missing_ids = [account_id for account_id in account_ids if account_id not in found_ids]
    for missing_id in missing_ids:
        skipped_count += 1
        skipped_list.append({
            'id': missing_id,
            'email': f'账号 #{missing_id}',
            'reason': '账号不存在或已删除'
        })

    for account in accounts:
        if (account['account_type'] or '').strip().lower() == 'imap':
            skipped_count += 1
            skipped_list.append({
                'id': account['id'],
                'email': account['email'],
                'reason': 'IMAP 账号无需刷新 Token'
            })
            continue

        result = refresh_outlook_account_token(account, 'manual')
        if result['success']:
            success_count += 1
            continue

        failed_count += 1
        failed_list.append({
            'id': account['id'],
            'email': account['email'],
            'error': result.get('error_message') or '未知错误'
        })

    requested_count = len(account_ids)
    processed_count = success_count + failed_count
    message_parts = [f'成功 {success_count}']
    if failed_count:
        message_parts.append(f'失败 {failed_count}')
    if skipped_count:
        message_parts.append(f'跳过 {skipped_count}')

    return jsonify({
        'success': True,
        'message': f'已处理 {requested_count} 个账号，' + '，'.join(message_parts),
        'requested_count': requested_count,
        'processed_count': processed_count,
        'success_count': success_count,
        'failed_count': failed_count,
        'skipped_count': skipped_count,
        'failed_list': failed_list,
        'skipped_list': skipped_list,
    })


def load_active_outlook_accounts_for_refresh(db_conn) -> List[sqlite3.Row]:
    cursor = db_conn.execute(
        '''
        SELECT id, email, client_id, refresh_token, group_id, status, account_type, provider
        FROM accounts
        WHERE status = 'active'
          AND COALESCE(account_type, 'outlook') = 'outlook'
        ORDER BY email COLLATE NOCASE ASC
        '''
    )
    return cursor.fetchall()


def load_selected_outlook_accounts_for_refresh(db_conn, account_ids: List[int]) -> List[sqlite3.Row]:
    if not account_ids:
        return []

    placeholders = ','.join('?' * len(account_ids))
    rows = db_conn.execute(
        f'''
        SELECT id, email, client_id, refresh_token, group_id, status, account_type, provider
        FROM accounts
        WHERE id IN ({placeholders})
        ''',
        tuple(account_ids)
    ).fetchall()
    order_map = {account_id: index for index, account_id in enumerate(account_ids)}
    return sorted(
        [row for row in rows if is_outlook_refreshable_account(row)],
        key=lambda row: order_map.get(row['id'], len(order_map)),
    )


def load_failed_outlook_accounts_for_refresh(db_conn) -> List[sqlite3.Row]:
    cursor = db_conn.execute(
        '''
        SELECT id, email, client_id, refresh_token, group_id, status, account_type, provider
        FROM accounts
        WHERE status = 'active'
          AND COALESCE(account_type, 'outlook') = 'outlook'
          AND COALESCE(NULLIF(last_refresh_status, ''), 'never') = 'failed'
        ORDER BY last_refresh_at DESC, id DESC
        '''
    )
    return cursor.fetchall()


def get_refresh_delay_seconds(db_conn) -> int:
    row = db_conn.execute(
        "SELECT value FROM settings WHERE key = 'refresh_delay_seconds'"
    ).fetchone()
    try:
        return max(0, min(60, int(row['value']) if row and row['value'] is not None else 5))
    except (TypeError, ValueError):
        return 5


def run_full_refresh(snapshot_trigger_type: str, log_refresh_type: str,
                     progress_callback=None, db_conn=None) -> Dict[str, Any]:
    lock_acquired = False
    owns_connection = db_conn is None
    conn = db_conn or sqlite3.connect(DATABASE)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    accounts: List[sqlite3.Row] = []
    delay_seconds = 0
    total = 0
    success_count = 0
    failed_count = 0
    failed_list: List[Dict[str, Any]] = []
    current_account = None
    current_account_counted = False

    try:
        acquire_token_refresh_run_lock()
        lock_acquired = True
        clear_token_refresh_stop_request()
        cleanup_refresh_logs(conn)
        conn.commit()

        accounts = load_active_outlook_accounts_for_refresh(conn)
        delay_seconds = get_refresh_delay_seconds(conn)
        total = len(accounts)

        mark_token_refresh_snapshot_running(snapshot_trigger_type, total, conn)
        conn.commit()

        if progress_callback:
            progress_callback({
                'type': 'start',
                'total': total,
                'delay_seconds': delay_seconds,
                'refresh_type': snapshot_trigger_type,
            })

        for index, account in enumerate(accounts, 1):
            if is_token_refresh_stop_requested():
                stopped_payload = finalize_stopped_full_refresh(
                    conn,
                    snapshot_trigger_type,
                    total,
                    success_count,
                    failed_count,
                    failed_list,
                    delay_seconds
                )
                if progress_callback:
                    progress_callback(stopped_payload)
                return stopped_payload

            current_account = account
            current_account_counted = False
            if progress_callback:
                progress_callback({
                    'type': 'progress',
                    'current': index,
                    'total': total,
                    'account_id': account['id'],
                    'email': account['email'],
                    'success_count': success_count,
                    'failed_count': failed_count,
                })

            result = refresh_outlook_account_token(account, log_refresh_type, db_conn=conn)
            conn.commit()

            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1
                failed_list.append({
                    'id': account['id'],
                    'email': account['email'],
                    'error': result.get('error_message') or '未知错误',
                })
            current_account_counted = True
            if progress_callback:
                progress_callback({
                    'type': 'account_result',
                    'current': index,
                    'total': total,
                    'account_id': account['id'],
                    'email': account['email'],
                    'status': 'success' if result.get('success') else 'failed',
                    'error_message': result.get('error_message') or '',
                    'success_count': success_count,
                    'failed_count': failed_count,
                })
            if is_token_refresh_stop_requested():
                stopped_payload = finalize_stopped_full_refresh(
                    conn,
                    snapshot_trigger_type,
                    total,
                    success_count,
                    failed_count,
                    failed_list,
                    delay_seconds
                )
                if progress_callback:
                    progress_callback(stopped_payload)
                return stopped_payload

            current_account = None

            if index < total and delay_seconds > 0:
                if progress_callback:
                    progress_callback({'type': 'delay', 'seconds': delay_seconds})
                if not wait_refresh_delay(delay_seconds):
                    stopped_payload = finalize_stopped_full_refresh(
                        conn,
                        snapshot_trigger_type,
                        total,
                        success_count,
                        failed_count,
                        failed_list,
                        delay_seconds
                    )
                    if progress_callback:
                        progress_callback(stopped_payload)
                    return stopped_payload

        error_summary = build_refresh_error_summary(failed_list)
        mark_token_refresh_snapshot_finished(
            snapshot_trigger_type,
            total,
            success_count,
            failed_count,
            error_summary,
            conn
        )
        conn.commit()

        result_payload = {
            'type': 'complete',
            'total': total,
            'success_count': success_count,
            'failed_count': failed_count,
            'failed_list': failed_list,
            'delay_seconds': delay_seconds,
            'refresh_type': snapshot_trigger_type,
        }
        if progress_callback:
            progress_callback(result_payload)
        return result_payload
    except TokenRefreshInProgressError:
        raise
    except Exception as exc:
        error_payload = finalize_aborted_full_refresh(
            conn,
            snapshot_trigger_type,
            log_refresh_type,
            total,
            success_count,
            failed_count,
            failed_list,
            current_account=current_account,
            current_account_counted=current_account_counted,
            error=exc
        )
        if progress_callback:
            progress_callback(error_payload)
        raise
    finally:
        if owns_connection:
            conn.close()
        clear_token_refresh_stop_request()
        release_token_refresh_run_lock(lock_acquired)


def stream_full_refresh_events(snapshot_trigger_type: str, log_refresh_type: str):
    import json

    conn = None
    lock_acquired = False
    accounts: List[sqlite3.Row] = []
    delay_seconds = 0
    total = 0
    success_count = 0
    failed_count = 0
    failed_list: List[Dict[str, Any]] = []
    current_account = None
    current_account_counted = False

    try:
        acquire_token_refresh_run_lock()
        lock_acquired = True
        clear_token_refresh_stop_request()
        conn = sqlite3.connect(DATABASE)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.row_factory = sqlite3.Row

        cleanup_refresh_logs(conn)
        conn.commit()

        accounts = load_active_outlook_accounts_for_refresh(conn)
        delay_seconds = get_refresh_delay_seconds(conn)
        total = len(accounts)

        mark_token_refresh_snapshot_running(snapshot_trigger_type, total, conn)
        conn.commit()
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'delay_seconds': delay_seconds, 'refresh_type': snapshot_trigger_type})}\n\n"

        for index, account in enumerate(accounts, 1):
            if is_token_refresh_stop_requested():
                stopped_payload = finalize_stopped_full_refresh(
                    conn,
                    snapshot_trigger_type,
                    total,
                    success_count,
                    failed_count,
                    failed_list,
                    delay_seconds
                )
                yield f"data: {json.dumps(stopped_payload)}\n\n"
                return

            current_account = account
            current_account_counted = False
            yield f"data: {json.dumps({'type': 'progress', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'success_count': success_count, 'failed_count': failed_count})}\n\n"

            result = refresh_outlook_account_token(account, log_refresh_type, db_conn=conn)
            conn.commit()

            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1
                failed_list.append({
                    'id': account['id'],
                    'email': account['email'],
                    'error': result.get('error_message') or '未知错误',
                })
            current_account_counted = True
            yield f"data: {json.dumps({'type': 'account_result', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'status': 'success' if result.get('success') else 'failed', 'error_message': result.get('error_message') or '', 'success_count': success_count, 'failed_count': failed_count})}\n\n"
            if is_token_refresh_stop_requested():
                stopped_payload = finalize_stopped_full_refresh(
                    conn,
                    snapshot_trigger_type,
                    total,
                    success_count,
                    failed_count,
                    failed_list,
                    delay_seconds
                )
                yield f"data: {json.dumps(stopped_payload)}\n\n"
                return

            current_account = None

            if index < total and delay_seconds > 0:
                yield f"data: {json.dumps({'type': 'delay', 'seconds': delay_seconds})}\n\n"
                if not wait_refresh_delay(delay_seconds):
                    stopped_payload = finalize_stopped_full_refresh(
                        conn,
                        snapshot_trigger_type,
                        total,
                        success_count,
                        failed_count,
                        failed_list,
                        delay_seconds
                    )
                    yield f"data: {json.dumps(stopped_payload)}\n\n"
                    return

        error_summary = build_refresh_error_summary(failed_list)
        mark_token_refresh_snapshot_finished(
            snapshot_trigger_type,
            total,
            success_count,
            failed_count,
            error_summary,
            conn
        )
        conn.commit()
        yield f"data: {json.dumps({'type': 'complete', 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'failed_list': failed_list, 'delay_seconds': delay_seconds, 'refresh_type': snapshot_trigger_type})}\n\n"
    except TokenRefreshInProgressError as exc:
        yield f"data: {json.dumps({'type': 'conflict', 'message': str(exc), 'refresh_type': snapshot_trigger_type})}\n\n"
    except Exception as exc:
        if conn is not None:
            error_payload = finalize_aborted_full_refresh(
                conn,
                snapshot_trigger_type,
                log_refresh_type,
                total,
                success_count,
                failed_count,
                failed_list,
                current_account=current_account,
                current_account_counted=current_account_counted,
                error=exc
            )
            yield f"data: {json.dumps(error_payload)}\n\n"
        else:
            failure_message = sanitize_error_details(str(exc)) or '未知错误'
            yield f"data: {json.dumps({'type': 'error', 'message': failure_message, 'refresh_type': snapshot_trigger_type})}\n\n"
    finally:
        if conn is not None:
            conn.close()
        clear_token_refresh_stop_request()
        release_token_refresh_run_lock(lock_acquired)


def stream_failed_refresh_events():
    import json

    conn = None
    lock_acquired = False
    accounts: List[sqlite3.Row] = []
    delay_seconds = 0
    total = 0
    success_count = 0
    failed_count = 0
    failed_list: List[Dict[str, Any]] = []
    current_account = None
    current_account_counted = False

    try:
        acquire_token_refresh_run_lock()
        lock_acquired = True
        clear_token_refresh_stop_request()
        conn = sqlite3.connect(DATABASE)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.row_factory = sqlite3.Row

        cleanup_refresh_logs(conn)
        conn.commit()

        accounts = load_failed_outlook_accounts_for_refresh(conn)
        delay_seconds = get_refresh_delay_seconds(conn)
        total = len(accounts)

        yield f"data: {json.dumps({'type': 'start', 'total': total, 'delay_seconds': delay_seconds, 'refresh_type': 'retry_failed'})}\n\n"

        for index, account in enumerate(accounts, 1):
            if is_token_refresh_stop_requested():
                yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='retry_failed'))}\n\n"
                return

            current_account = account
            current_account_counted = False
            yield f"data: {json.dumps({'type': 'progress', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'success_count': success_count, 'failed_count': failed_count})}\n\n"

            result = refresh_outlook_account_token(account, 'retry', db_conn=conn)
            conn.commit()

            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1
                failed_list.append({
                    'id': account['id'],
                    'email': account['email'],
                    'error': result.get('error_message') or '未知错误',
                })
            current_account_counted = True

            yield f"data: {json.dumps({'type': 'account_result', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'status': 'success' if result.get('success') else 'failed', 'error_message': result.get('error_message') or '', 'success_count': success_count, 'failed_count': failed_count})}\n\n"

            if is_token_refresh_stop_requested():
                yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='retry_failed'))}\n\n"
                return

            current_account = None

            if index < total and delay_seconds > 0:
                yield f"data: {json.dumps({'type': 'delay', 'seconds': delay_seconds, 'refresh_type': 'retry_failed'})}\n\n"
                if not wait_refresh_delay(delay_seconds):
                    yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='retry_failed'))}\n\n"
                    return

        yield f"data: {json.dumps({'type': 'complete', 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'failed_list': failed_list, 'delay_seconds': delay_seconds, 'refresh_type': 'retry_failed'})}\n\n"
    except TokenRefreshInProgressError as exc:
        yield f"data: {json.dumps({'type': 'conflict', 'message': str(exc), 'refresh_type': 'retry_failed'})}\n\n"
    except Exception as exc:
        if conn is not None:
            error_payload = finalize_aborted_retry_refresh(
                conn,
                'retry',
                total,
                success_count,
                failed_count,
                failed_list,
                current_account=current_account,
                current_account_counted=current_account_counted,
                error=exc
            )
            yield f"data: {json.dumps(error_payload)}\n\n"
        else:
            failure_message = sanitize_error_details(str(exc)) or '未知错误'
            yield f"data: {json.dumps({'type': 'error', 'message': failure_message, 'refresh_type': 'retry_failed'})}\n\n"
    finally:
        if conn is not None:
            conn.close()
        clear_token_refresh_stop_request()
        release_token_refresh_run_lock(lock_acquired)


def normalize_refresh_account_ids(raw_account_ids: Any) -> List[int]:
    if isinstance(raw_account_ids, str):
        candidates = raw_account_ids.replace('\n', ',').split(',')
    elif isinstance(raw_account_ids, list):
        candidates = raw_account_ids
    else:
        candidates = []

    account_ids = []
    seen_ids = set()
    for account_id in candidates:
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            continue
        if normalized_id <= 0 or normalized_id in seen_ids:
            continue
        seen_ids.add(normalized_id)
        account_ids.append(normalized_id)
    return account_ids


def prune_expired_selected_refresh_tasks(now: float) -> None:
    expired_before = now - SELECTED_REFRESH_TASK_TTL_SECONDS
    expired_task_ids = [
        task_id
        for task_id, task in selected_refresh_tasks.items()
        if float(task.get('created_at') or 0) < expired_before
    ]
    for task_id in expired_task_ids:
        selected_refresh_tasks.pop(task_id, None)


def create_selected_refresh_task(account_ids: List[int]) -> str:
    now = time.time()
    with selected_refresh_tasks_lock:
        prune_expired_selected_refresh_tasks(now)
        while True:
            task_id = secrets.token_urlsafe(24)
            if task_id not in selected_refresh_tasks:
                break
        selected_refresh_tasks[task_id] = {
            'account_ids': list(account_ids),
            'created_at': now,
        }
    return task_id


def pop_selected_refresh_task(task_id: str) -> Optional[List[int]]:
    normalized_task_id = str(task_id or '').strip()
    if not normalized_task_id:
        return None

    now = time.time()
    with selected_refresh_tasks_lock:
        prune_expired_selected_refresh_tasks(now)
        task = selected_refresh_tasks.pop(normalized_task_id, None)

    if not task:
        return None
    return normalize_refresh_account_ids(task.get('account_ids') or [])


def stream_selected_refresh_task_events(task_id: str):
    import json

    account_ids = pop_selected_refresh_task(task_id)
    if account_ids is None:
        payload = {
            'type': 'error',
            'message': '刷新任务不存在或已过期',
            'refresh_type': 'manual_selected',
        }
        yield f"data: {json.dumps(payload)}\n\n"
        return

    yield from stream_selected_refresh_events(account_ids)


def stream_selected_refresh_events(account_ids: List[int]):
    import json

    conn = None
    lock_acquired = False
    accounts: List[sqlite3.Row] = []
    delay_seconds = 0
    total = 0
    success_count = 0
    failed_count = 0
    failed_list: List[Dict[str, Any]] = []
    current_account = None
    current_account_counted = False

    if not account_ids:
        yield f"data: {json.dumps({'type': 'error', 'message': '请选择要刷新的账号', 'refresh_type': 'manual_selected'})}\n\n"
        return

    try:
        acquire_token_refresh_run_lock()
        lock_acquired = True
        clear_token_refresh_stop_request()
        conn = sqlite3.connect(DATABASE)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.row_factory = sqlite3.Row

        cleanup_refresh_logs(conn)
        conn.commit()

        accounts = load_selected_outlook_accounts_for_refresh(conn, account_ids)
        delay_seconds = get_refresh_delay_seconds(conn)
        total = len(accounts)

        yield f"data: {json.dumps({'type': 'start', 'total': total, 'delay_seconds': delay_seconds, 'refresh_type': 'manual_selected'})}\n\n"

        for index, account in enumerate(accounts, 1):
            if is_token_refresh_stop_requested():
                yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='manual_selected'))}\n\n"
                return

            current_account = account
            current_account_counted = False
            yield f"data: {json.dumps({'type': 'progress', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'success_count': success_count, 'failed_count': failed_count})}\n\n"

            result = refresh_outlook_account_token(account, 'manual_selected', db_conn=conn)
            conn.commit()

            if result.get('success'):
                success_count += 1
            else:
                failed_count += 1
                failed_list.append({
                    'id': account['id'],
                    'email': account['email'],
                    'error': result.get('error_message') or '未知错误',
                })
            current_account_counted = True

            yield f"data: {json.dumps({'type': 'account_result', 'current': index, 'total': total, 'account_id': account['id'], 'email': account['email'], 'status': 'success' if result.get('success') else 'failed', 'error_message': result.get('error_message') or '', 'success_count': success_count, 'failed_count': failed_count})}\n\n"

            if is_token_refresh_stop_requested():
                yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='manual_selected'))}\n\n"
                return

            current_account = None

            if index < total and delay_seconds > 0:
                yield f"data: {json.dumps({'type': 'delay', 'seconds': delay_seconds, 'refresh_type': 'manual_selected'})}\n\n"
                if not wait_refresh_delay(delay_seconds):
                    yield f"data: {json.dumps(build_stopped_refresh_payload(total, success_count, failed_count, failed_list, delay_seconds=delay_seconds, refresh_type='manual_selected'))}\n\n"
                    return

        yield f"data: {json.dumps({'type': 'complete', 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'failed_list': failed_list, 'delay_seconds': delay_seconds, 'refresh_type': 'manual_selected'})}\n\n"
    except TokenRefreshInProgressError as exc:
        yield f"data: {json.dumps({'type': 'conflict', 'message': str(exc), 'refresh_type': 'manual_selected'})}\n\n"
    except Exception as exc:
        failure_message = sanitize_error_details(str(exc)) or '未知错误'
        if conn is not None and current_account is not None and not current_account_counted:
            failed_count += 1
            failed_list.append({
                'id': current_account['id'],
                'email': current_account['email'],
                'error': failure_message,
            })
            try:
                log_refresh_result(
                    current_account['id'],
                    current_account['email'],
                    'manual_selected',
                    'failed',
                    failure_message,
                    db_conn=conn
                )
                conn.commit()
            except Exception as log_error:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"记录异常批量刷新结果失败: {str(log_error)}")
        yield f"data: {json.dumps({'type': 'error', 'message': failure_message, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'failed_list': failed_list, 'refresh_type': 'manual_selected'})}\n\n"
    finally:
        if conn is not None:
            conn.close()
        clear_token_refresh_stop_request()
        release_token_refresh_run_lock(lock_acquired)


@app.route('/api/accounts/refresh-all', methods=['GET'])
@login_required
def api_refresh_all_accounts():
    """刷新所有账号的 token（流式响应，实时返回进度）"""
    return Response(stream_full_refresh_events('manual_all', 'manual'), mimetype='text/event-stream')


@app.route('/api/accounts/<int:account_id>/retry-refresh', methods=['POST'])
@login_required
def api_retry_refresh_account(account_id):
    """重试单个失败账号的刷新"""
    return api_refresh_account(account_id)


@app.route('/api/accounts/refresh-failed-stream', methods=['GET'])
@login_required
def api_refresh_failed_accounts_stream():
    """流式重试所有失败账号"""
    return Response(stream_failed_refresh_events(), mimetype='text/event-stream')


@app.route('/api/accounts/refresh-selected-stream', methods=['POST'])
@login_required
def api_create_refresh_selected_accounts_stream_task():
    """初始化选中账号流式刷新任务。"""
    data = request.get_json(silent=True) or {}
    account_ids = normalize_refresh_account_ids(data.get('account_ids') or [])
    if not account_ids:
        return jsonify({'success': False, 'error': '请选择要刷新的账号'})

    task_id = create_selected_refresh_task(account_ids)
    return jsonify({
        'success': True,
        'task_id': task_id,
        'stream_url': f'/api/accounts/refresh-selected-stream/{task_id}',
    })


@app.route('/api/accounts/refresh-selected-stream/<task_id>', methods=['GET'])
@login_required
def api_refresh_selected_accounts_task_stream(task_id):
    """订阅选中账号流式刷新任务。"""
    return Response(stream_selected_refresh_task_events(task_id), mimetype='text/event-stream')


@app.route('/api/accounts/refresh-selected-stream', methods=['GET'])
@login_required
def api_refresh_selected_accounts_stream():
    """选中账号流式刷新需先通过 POST 初始化任务。"""
    return Response(stream_selected_refresh_task_events(''), mimetype='text/event-stream')


@app.route('/api/accounts/refresh-failed', methods=['POST'])
@login_required
def api_refresh_failed_accounts():
    """重试所有失败的账号"""
    cleanup_refresh_logs()
    db = get_db()
    accounts = load_failed_outlook_accounts_for_refresh(db)

    success_count = 0
    failed_count = 0
    failed_list = []

    for account in accounts:
        result = refresh_outlook_account_token(account, 'retry')
        if result['success']:
            success_count += 1
        else:
            failed_count += 1
            failed_list.append({
                'id': account['id'],
                'email': account['email'],
                'error': result.get('error_message') or '未知错误'
            })

    return jsonify({
        'success': True,
        'total': len(accounts),
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list
    })


@app.route('/api/accounts/trigger-scheduled-refresh', methods=['GET'])
@login_required
def api_trigger_scheduled_refresh():
    """手动触发定时刷新（支持强制刷新）"""
    import json
    from datetime import datetime, timedelta

    force = request.args.get('force', 'false').lower() == 'true'

    # 获取配置
    refresh_interval_days = int(get_setting('refresh_interval_days', '30'))

    last_refresh = build_refresh_stats(get_db()).get('last_refresh_time')

    # 判断是否需要刷新（force=true 时跳过检查）
    if not force and last_refresh:
        last_refresh_time = datetime.fromisoformat(last_refresh)
        next_refresh_time = last_refresh_time + timedelta(days=refresh_interval_days)
        if datetime.now() < next_refresh_time:
            return jsonify({
                'success': False,
                'message': f'距离上次刷新未满 {refresh_interval_days} 天，下次刷新时间：{next_refresh_time.strftime("%Y-%m-%d %H:%M:%S")}',
                'last_refresh': last_refresh,
                'next_refresh': next_refresh_time.isoformat()
            })

    # 执行刷新（使用流式响应）
    snapshot_type = 'manual_all' if force else 'scheduled'
    log_type = 'manual' if force else 'scheduled'
    return Response(stream_full_refresh_events(snapshot_type, log_type), mimetype='text/event-stream')


@app.route('/api/accounts/stop-full-refresh', methods=['POST'])
@login_required
def api_stop_full_refresh():
    """请求停止当前全量刷新任务。"""
    if not token_refresh_run_lock.locked():
        clear_token_refresh_stop_request()
        return jsonify({
            'success': False,
            'message': '当前没有进行中的全量刷新任务'
        }), 409

    request_token_refresh_stop()
    return jsonify({
        'success': True,
        'message': TOKEN_REFRESH_STOP_REQUESTED_MESSAGE,
    })



def serialize_refresh_log_row(row):
    account_email = row['current_account_email'] or row['log_account_email']
    return {
        'id': row['id'],
        'account_id': row['account_id'],
        'account_email': account_email,
        'refresh_type': row['refresh_type'],
        'status': row['status'],
        'error_message': row['error_message'],
        'created_at': row['created_at']
    }


@app.route('/api/accounts/refresh-logs', methods=['GET'])
@login_required
def api_get_refresh_logs():
    """获取所有账号的刷新历史（只返回全量刷新：manual 和 scheduled，近半年）"""
    db = get_db()
    limit, offset = parse_log_pagination(
        request.args.get('limit'),
        request.args.get('offset'),
    )

    cursor = db.execute('''
        SELECT
            l.id,
            l.account_id,
            l.account_email AS log_account_email,
            a.email AS current_account_email,
            l.refresh_type,
            l.status,
            l.error_message,
            l.created_at
        FROM account_refresh_logs l
        LEFT JOIN accounts a ON l.account_id = a.id
        WHERE l.refresh_type IN ('manual', 'scheduled')
        AND l.created_at >= datetime('now', '-6 months')
        ORDER BY l.created_at DESC
        LIMIT ? OFFSET ?
    ''', (limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append(serialize_refresh_log_row(row))

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/<int:account_id>/refresh-logs', methods=['GET'])
@login_required
def api_get_account_refresh_logs(account_id):
    """获取单个账号的刷新历史"""
    db = get_db()
    limit, offset = parse_log_pagination(
        request.args.get('limit'),
        request.args.get('offset'),
        default_limit=50,
    )

    cursor = db.execute('''
        SELECT
            l.id,
            l.account_id,
            l.account_email AS log_account_email,
            a.email AS current_account_email,
            l.refresh_type,
            l.status,
            l.error_message,
            l.created_at
        FROM account_refresh_logs l
        LEFT JOIN accounts a ON l.account_id = a.id
        WHERE l.account_id = ?
        ORDER BY l.created_at DESC
        LIMIT ? OFFSET ?
    ''', (account_id, limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append(serialize_refresh_log_row(row))

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/refresh-logs/failed', methods=['GET'])
@login_required
def api_get_failed_refresh_logs():
    """获取所有失败的刷新记录"""
    db = get_db()

    cursor = db.execute('''
        SELECT
            a.id AS account_id,
            a.email AS account_email,
            a.status AS account_status,
            a.last_refresh_status AS status,
            a.last_refresh_error AS error_message,
            a.last_refresh_at AS created_at
        FROM accounts a
        WHERE a.status = 'active'
          AND COALESCE(a.account_type, 'outlook') = 'outlook'
          AND COALESCE(NULLIF(a.last_refresh_status, ''), 'never') = 'failed'
        ORDER BY a.last_refresh_at DESC, a.id DESC
    ''')

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['account_id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'],
            'account_status': row['account_status'],
            'refresh_type': 'latest',
            'status': row['status'] or 'failed',
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/forwarding-logs', methods=['GET'])
@login_required
def api_get_forwarding_logs():
    """获取最近的转发记录"""
    db = get_db()
    limit, offset = parse_log_pagination(
        request.args.get('limit'),
        request.args.get('offset'),
    )

    cursor = db.execute('''
        SELECT * FROM forwarding_logs
        WHERE created_at >= datetime('now', '-6 months')
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', (limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'],
            'message_id': row['message_id'],
            'channel': row['channel'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/forwarding-logs/failed', methods=['GET'])
@login_required
def api_get_failed_forwarding_logs():
    """获取最近失败的转发记录"""
    db = get_db()
    limit, offset = parse_log_pagination(
        request.args.get('limit'),
        request.args.get('offset'),
    )

    cursor = db.execute('''
        SELECT * FROM forwarding_logs
        WHERE status = 'failed'
        AND created_at >= datetime('now', '-6 months')
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', (limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'],
            'message_id': row['message_id'],
            'channel': row['channel'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/<int:account_id>/forwarding-logs', methods=['GET'])
@login_required
def api_get_account_forwarding_logs(account_id):
    """获取单个账号的转发记录"""
    db = get_db()
    account = get_account_by_id(account_id)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'}), 404
    limit, offset = parse_log_pagination(
        request.args.get('limit'),
        request.args.get('offset'),
    )
    failed_only = str(request.args.get('failed_only', '')).strip().lower() in ('1', 'true', 'yes', 'on')

    query = '''
        SELECT * FROM forwarding_logs
        WHERE account_id = ?
        AND created_at >= datetime('now', '-6 months')
    '''
    params = [account_id]
    if failed_only:
        query += " AND status = 'failed'"
    query += '''
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    '''
    params.extend([limit, offset])

    cursor = db.execute(query, tuple(params))

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'],
            'message_id': row['message_id'],
            'channel': row['channel'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({
        'success': True,
        'logs': logs,
        'account': {
            'id': account['id'],
            'email': account.get('email', ''),
            'status': account.get('status', 'active'),
            'forward_enabled': bool(account.get('forward_enabled')),
            'forward_last_checked_at': account.get('forward_last_checked_at', ''),
        }
    })


@app.route('/api/accounts/refresh-stats', methods=['GET'])
@login_required
def api_get_refresh_stats():
    """获取刷新统计信息（统计当前失败状态的邮箱数量）"""
    return jsonify({'success': True, 'stats': build_refresh_stats()})


@app.route('/api/accounts/refresh-status-list', methods=['GET'])
@login_required
def api_get_refresh_status_list():
    """获取 Token 刷新管理所需的账号状态列表。"""
    q = request.args.get('q', '')
    status = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 100, type=int)

    payload = query_refreshable_accounts(
        get_db(),
        search=q,
        refresh_status=status,
        page=page,
        page_size=page_size,
    )
    payload['stats'] = build_refresh_stats(get_db())
    return jsonify({'success': True, **payload})


# ==================== 邮件 API ====================



# ==================== Email Deletion Helpers ====================

def delete_emails_graph(client_id: str, refresh_token: str, message_ids: List[str], proxy_url: str = None,
                        fallback_proxy_urls: List[str] = None) -> Dict[str, Any]:
    """通过 Graph API 批量删除邮件（永久删除）"""
    access_token = get_access_token_graph(client_id, refresh_token, proxy_url, fallback_proxy_urls)
    if not access_token:
        return {"success": False, "error": "获取 Access Token 失败"}

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    # Graph API 不支持一次性批量删除所有邮件，需要逐个删除
    # 但可以使用 batch 请求来优化
    # https://learn.microsoft.com/en-us/graph/json-batching
    
    # 限制每批次请求数量（Graph API 限制为 20）
    BATCH_SIZE = 20
    success_count = 0
    failed_count = 0
    errors = []

    for i in range(0, len(message_ids), BATCH_SIZE):
        batch = message_ids[i:i + BATCH_SIZE]
        
        # 构造 batch 请求 body
        batch_requests = []
        for idx, msg_id in enumerate(batch):
            batch_requests.append({
                "id": str(idx),
                "method": "DELETE",
                "url": f"/me/messages/{msg_id}"
            })
        
        try:
            response = request_with_proxy_failover(
                'post',
                "https://graph.microsoft.com/v1.0/$batch",
                headers=headers,
                json={"requests": batch_requests},
                timeout=30,
                proxy_url=proxy_url,
                fallback_proxy_urls=fallback_proxy_urls,
            )
            
            if response.status_code == 200:
                results = response.json().get("responses", [])
                for res in results:
                    if res.get("status") in [200, 204]:
                        success_count += 1
                    else:
                        failed_count += 1
                        # 记录具体错误
                        errors.append(f"Msg ID: {batch[int(res['id'])]}, Status: {res.get('status')}")
            else:
                failed_count += len(batch)
                errors.append(f"Batch request failed: {response.text}")
                
        except Exception as e:
            failed_count += len(batch)
            errors.append(f"Network error: {str(e)}")

    return {
        "success": failed_count == 0,
        "success_count": success_count,
        "failed_count": failed_count,
        "errors": errors
    }

def delete_emails_imap(email_addr: str, client_id: str, refresh_token: str, message_ids: List[str], server: str,
                       proxy_url: str = None, fallback_proxy_urls: List[str] = None) -> Dict[str, Any]:
    """通过 IMAP 删除邮件（永久删除）"""
    access_token = get_access_token_imap(client_id, refresh_token, proxy_url, fallback_proxy_urls)
    if not access_token:
        return {"success": False, "error": "获取 Access Token 失败"}
        
    try:
        # 生成 OAuth2 认证字符串
        auth_string = 'user=%s\x01auth=Bearer %s\x01\x01' % (email_addr, access_token)
        
        # 连接 IMAP
        with proxy_socket_context(proxy_url):
            imap = imaplib.IMAP4_SSL(server, IMAP_PORT, timeout=IMAP_TIMEOUT)
        imap.authenticate('XOAUTH2', lambda x: auth_string.encode('utf-8'))
        
        # 选择文件夹
        imap.select('INBOX')
        
        # IMAP 删除需要 UID。如果我们没有 UID，这很难。
        # 鉴于我们只实现了 Graph 删除，并且 fallback 到 IMAP 比较复杂，
        # 这里暂时返回不支持，或仅做简单的尝试（如果 ID 恰好是 UID）
        # 但通常 Graph ID 不是 UID。
        
        return {"success": False, "error": "IMAP 删除暂不支持 (ID 格式不兼容)"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}


VALID_MAIL_FOLDERS = {'inbox', 'junkemail', 'deleteditems', 'all'}


def normalize_folder_name(folder: str) -> str:
    value = (folder or 'inbox').strip().lower()
    if value in {'both', 'combined'}:
        return 'all'
    if value in {'trash'}:
        return 'deleteditems'
    return value or 'inbox'


def get_query_arg_preserve_plus(name: str, default: str = '') -> str:
    raw_query = request.query_string.decode('utf-8', errors='ignore')
    if raw_query:
        for chunk in raw_query.split('&'):
            if not chunk:
                continue
            key, sep, value = chunk.partition('=')
            if unquote(key) == name:
                return unquote(value) if sep else ''
    return request.args.get(name, default)


def normalize_email_action_items(raw_items: Any, fallback_folder: str = 'inbox') -> List[Dict[str, str]]:
    normalized_items: List[Dict[str, str]] = []
    for raw_item in raw_items or []:
        if isinstance(raw_item, dict):
            message_id = str(raw_item.get('id') or raw_item.get('message_id') or '').strip()
            folder = normalize_folder_name(raw_item.get('folder', fallback_folder))
            id_mode = str(raw_item.get('id_mode') or '').strip().lower()
        else:
            message_id = str(raw_item or '').strip()
            folder = normalize_folder_name(fallback_folder)
            id_mode = ''

        if not message_id:
            continue

        normalized_items.append({
            'id': message_id,
            'folder': folder,
            'id_mode': id_mode,
        })
    return normalized_items


def merge_email_action_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged_updated_ids: List[str] = []
    merged_errors: List[Any] = []
    success_count = 0
    failed_count = 0

    for result in results or []:
        success_count += int(result.get('success_count', 0) or 0)
        failed_count += int(result.get('failed_count', 0) or 0)
        merged_updated_ids.extend([str(item) for item in (result.get('updated_ids') or []) if str(item)])
        merged_errors.extend(result.get('errors') or [])

    deduped_updated_ids = list(dict.fromkeys(merged_updated_ids))
    merged_result = {
        'success': failed_count == 0,
        'success_count': success_count,
        'failed_count': failed_count,
        'updated_ids': deduped_updated_ids,
        'errors': merged_errors,
    }
    if merged_errors:
        merged_result['error'] = merged_errors[0]
    return merged_result


def mark_retained_normal_mail_rows_read(account: Dict[str, Any], items: List[Dict[str, str]],
                                        result: Dict[str, Any], fallback_id_mode: str = '',
                                        db=None) -> int:
    account_id = int((account or {}).get('id') or 0)
    updated_ids = {
        str(message_id or '').strip()
        for message_id in (result or {}).get('updated_ids') or []
        if str(message_id or '').strip()
    }
    if not account_id or not updated_ids:
        return 0

    keys = []
    seen_keys = set()
    for item in items or []:
        message_id = str((item or {}).get('id') or '').strip()
        if message_id not in updated_ids:
            continue

        folder = normalize_folder_name((item or {}).get('folder', 'inbox'))
        id_mode = str((item or {}).get('id_mode') or fallback_id_mode or '').strip().lower()
        if not id_mode:
            continue

        key = (account_id, folder, message_id, id_mode)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        keys.append(key)

    if not keys:
        return 0

    database = db or get_db()
    updated_count = 0
    for key in keys:
        cursor = database.execute(
            '''
            UPDATE retained_normal_mail_messages
            SET is_read = 1, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = ?
            ''',
            key
        )
        updated_count += max(0, cursor.rowcount or 0)
    database.commit()
    return updated_count


def get_successfully_deleted_message_ids(result: Dict[str, Any], requested_ids: List[str]) -> List[str]:
    if not (result or {}).get('success'):
        return []

    for field_name in ('deleted_ids', 'updated_ids'):
        explicit_ids = [
            str(message_id or '').strip()
            for message_id in (result or {}).get(field_name) or []
            if str(message_id or '').strip()
        ]
        if explicit_ids:
            return list(dict.fromkeys(explicit_ids))

    normalized_requested = [
        str(message_id or '').strip()
        for message_id in requested_ids or []
        if str(message_id or '').strip()
    ]
    if not normalized_requested:
        return []

    success_count = int((result or {}).get('success_count', len(normalized_requested)) or 0)
    failed_count = int((result or {}).get('failed_count', 0) or 0)
    if failed_count == 0 and success_count == len(normalized_requested):
        return list(dict.fromkeys(normalized_requested))
    return []


def delete_retained_normal_mail_rows(account: Dict[str, Any], requested_ids: List[str],
                                     result: Dict[str, Any], fallback_id_mode: str = '',
                                     db=None) -> int:
    account_id = int((account or {}).get('id') or 0)
    deleted_ids = get_successfully_deleted_message_ids(result, requested_ids)
    if not account_id or not deleted_ids:
        return 0

    database = db or get_db()
    id_mode = str(fallback_id_mode or '').strip().lower()
    deleted_count = 0
    for message_id in deleted_ids:
        if id_mode:
            cursor = database.execute(
                '''
                DELETE FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ? AND id_mode = ?
                ''',
                (account_id, message_id, id_mode)
            )
        else:
            cursor = database.execute(
                '''
                DELETE FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''',
                (account_id, message_id)
            )
        deleted_count += max(0, cursor.rowcount or 0)
    database.commit()
    return deleted_count


def split_email_action_items_by_method(items: List[Dict[str, str]], method: str) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    graph_items = []
    imap_items = []
    for item in items:
        id_mode = str(item.get('id_mode') or '').strip().lower()
        if id_mode == 'graph':
            graph_items.append(item)
        elif id_mode in {'uid', 'sequence'}:
            imap_items.append(item)
        elif method == 'graph':
            graph_items.append(item)
        else:
            imap_items.append(item)
    return graph_items, imap_items


def mark_imap_account_emails_read(account: Dict[str, Any], items: List[Dict[str, str]],
                                  proxy_url: str) -> Dict[str, Any]:
    result = mark_emails_read_imap_generic_result(
        account['email'],
        account.get('imap_password', ''),
        account.get('imap_host', ''),
        items,
        account.get('imap_port', 993),
        account.get('provider', 'custom'),
        proxy_url
    )
    mark_retained_normal_mail_rows_read(account, items, result, fallback_id_mode='uid')
    return result


def mark_graph_items_read(account: Dict[str, Any], items: List[Dict[str, str]],
                          proxy_url: str, fallback_proxy_urls: List[str]) -> Dict[str, Any]:
    result = mark_emails_read_graph_result(
        account['client_id'],
        account['refresh_token'],
        [item['id'] for item in items],
        proxy_url,
        fallback_proxy_urls,
    )
    mark_retained_normal_mail_rows_read(account, items, result, fallback_id_mode='graph')
    return result


def mark_oauth_imap_items_read(account: Dict[str, Any], items: List[Dict[str, str]],
                               proxy_url: str, fallback_proxy_urls: List[str]) -> Dict[str, Any]:
    result = mark_emails_read_imap_batch(
        account['email'], account['client_id'], account['refresh_token'],
        items, IMAP_SERVER_NEW, proxy_url, fallback_proxy_urls,
    )
    if not result.get('success') and result.get('success_count', 0) == 0:
        result = mark_emails_read_imap_batch(
            account['email'], account['client_id'], account['refresh_token'],
            items, IMAP_SERVER_OLD, proxy_url, fallback_proxy_urls,
        )
    mark_retained_normal_mail_rows_read(account, items, result, fallback_id_mode='uid')
    return result


def normalize_email_list_item(item: Dict[str, Any], folder: str) -> Dict[str, Any]:
    row = dict(item or {})
    row['subject'] = row.get('subject', '无主题')
    row['from'] = row.get('from', '未知')
    row['to'] = str(row.get('to', '') or '')
    row['date'] = row.get('date', '')
    row['is_read'] = bool(row.get('is_read', False))
    row['has_attachments'] = bool(row.get('has_attachments', False))
    row['body_preview'] = row.get('body_preview', '')
    row['folder'] = row.get('folder') or folder
    row['id_mode'] = row.get('id_mode', '')
    return row


def coerce_retained_mail_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, tuple, set)):
        return ', '.join(str(item).strip() for item in value if str(item).strip())
    return str(value)


def coerce_retained_mail_bool(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {'1', 'true', 'yes', 'on'} else 0
    return 1 if bool(value) else 0


def retained_mail_storage_folder(item: Dict[str, Any], request_folder: str) -> str:
    folder_name = normalize_folder_name(request_folder)
    if folder_name == 'all':
        return normalize_folder_name((item or {}).get('folder', 'all'))
    return folder_name


def build_retained_normal_mail_list_row(account_id: int, item: Dict[str, Any],
                                        request_folder: str) -> Optional[Dict[str, Any]]:
    source = dict(item or {})
    provider_message_id = str(source.get('id') or '').strip()
    if not provider_message_id:
        return None

    storage_folder = retained_mail_storage_folder(source, request_folder)
    if not source.get('from') and source.get('sender'):
        source['from'] = source.get('sender')
    if not source.get('to') and source.get('recipients'):
        source['to'] = coerce_retained_mail_text(source.get('recipients'))
    if not source.get('date') and source.get('received_at'):
        source['date'] = source.get('received_at')

    normalized = normalize_email_list_item(source, storage_folder)
    normalized['folder'] = storage_folder
    return {
        'account_id': account_id,
        'folder': normalized['folder'],
        'provider_message_id': provider_message_id,
        'id_mode': str(normalized.get('id_mode') or '').strip().lower(),
        'subject': coerce_retained_mail_text(normalized.get('subject')) or '无主题',
        'sender': coerce_retained_mail_text(normalized.get('from')) or '未知',
        'recipients': coerce_retained_mail_text(normalized.get('to')),
        'received_at': coerce_retained_mail_text(normalized.get('date')),
        'received_at_sort': retained_mail_received_at_sort(normalized.get('date')),
        'is_read': coerce_retained_mail_bool(normalized.get('is_read')),
        'has_attachments': coerce_retained_mail_bool(normalized.get('has_attachments')),
        'body_preview': coerce_retained_mail_text(normalized.get('body_preview')),
    }


RETAINED_MAIL_KEY_LOOKUP_CHUNK_SIZE = 200
RETAINED_MAIL_BODY_FETCH_LIMIT = 5


NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE: Optional[bool] = None


def normalize_setting_bool(value: Any) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def set_normal_mail_local_retention_enabled_cache(value: Any) -> bool:
    global NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE
    NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE = normalize_setting_bool(value)
    return NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE


def clear_normal_mail_local_retention_enabled_cache():
    global NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE
    NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE = None


def is_normal_mail_local_retention_enabled() -> bool:
    if NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE is None:
        set_normal_mail_local_retention_enabled_cache(
            get_setting('normal_mail_local_retention_enabled', 'false')
        )
    return bool(NORMAL_MAIL_LOCAL_RETENTION_SETTING_CACHE)


def retained_normal_mail_key(row: Dict[str, Any]) -> tuple:
    return (
        int(row.get('account_id') or 0),
        str(row.get('folder') or ''),
        str(row.get('provider_message_id') or ''),
        str(row.get('id_mode') or '').strip().lower(),
    )


def retained_mail_received_at_sort(value: Any) -> float:
    parsed = parse_email_datetime(str(value or ''))
    if not parsed:
        return 0.0
    return parsed.timestamp()


def retained_mail_new_message_identifier(row: Dict[str, Any]) -> Dict[str, str]:
    return {
        'id': str(row.get('provider_message_id') or ''),
        'folder': str(row.get('folder') or ''),
        'id_mode': str(row.get('id_mode') or ''),
    }


def query_existing_retained_normal_mail_keys(rows: List[Dict[str, Any]], db=None) -> set:
    if not rows:
        return set()

    database = db or get_db()
    existing_keys = set()
    for index in range(0, len(rows), RETAINED_MAIL_KEY_LOOKUP_CHUNK_SIZE):
        chunk = rows[index:index + RETAINED_MAIL_KEY_LOOKUP_CHUNK_SIZE]
        clauses = []
        params: List[Any] = [int(chunk[0]['account_id'])]
        for row in chunk:
            clauses.append('(folder = ? AND provider_message_id = ? AND id_mode = ?)')
            params.extend([row['folder'], row['provider_message_id'], row['id_mode']])
        result_rows = database.execute(
            f'''
            SELECT account_id, folder, provider_message_id, id_mode
            FROM retained_normal_mail_messages
            WHERE account_id = ? AND ({' OR '.join(clauses)})
            ''',
            params
        ).fetchall()
        existing_keys.update(retained_normal_mail_key(dict(row)) for row in result_rows)
    return existing_keys


def find_new_retained_normal_mail_identifiers(account: Dict[str, Any], folder: str,
                                              items: List[Dict[str, Any]], db=None) -> List[Dict[str, str]]:
    account_id = int((account or {}).get('id') or 0)
    if not account_id:
        return []

    unique_rows = []
    seen_keys = set()
    for item in items or []:
        row = build_retained_normal_mail_list_row(account_id, item, folder)
        if row is None:
            continue
        key = retained_normal_mail_key(row)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_rows.append(row)

    existing_keys = query_existing_retained_normal_mail_keys(unique_rows, db)
    return [
        retained_mail_new_message_identifier(row)
        for row in unique_rows
        if retained_normal_mail_key(row) not in existing_keys
    ]


def upsert_retained_normal_mail_list_items(account: Dict[str, Any], folder: str,
                                           items: List[Dict[str, Any]], db=None) -> int:
    account_id = int((account or {}).get('id') or 0)
    if not account_id:
        return 0

    rows = [
        row for row in (
            build_retained_normal_mail_list_row(account_id, item, folder)
            for item in (items or [])
        )
        if row is not None
    ]
    if not rows:
        return 0

    database = db or get_db()
    database.executemany(
        '''
        INSERT INTO retained_normal_mail_messages (
            account_id, folder, provider_message_id, id_mode,
            subject, sender, recipients, received_at, received_at_sort,
            is_read, has_attachments, body_preview,
            list_cached, list_cached_at, last_synced_at, updated_at
        )
        VALUES (
            :account_id, :folder, :provider_message_id, :id_mode,
            :subject, :sender, :recipients, :received_at, :received_at_sort,
            :is_read, :has_attachments, :body_preview,
            1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT(account_id, folder, provider_message_id, id_mode)
        DO UPDATE SET
            subject = excluded.subject,
            sender = excluded.sender,
            recipients = excluded.recipients,
            received_at = excluded.received_at,
            received_at_sort = excluded.received_at_sort,
            is_read = excluded.is_read,
            has_attachments = excluded.has_attachments,
            body_preview = excluded.body_preview,
            list_cached = 1,
            list_cached_at = CURRENT_TIMESTAMP,
            last_synced_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        ''',
        rows
    )
    database.commit()
    return len(rows)



RETAINED_MAIL_ATTACHMENT_METADATA_KEYS = (
    'id', 'name', 'content_type', 'contentType', 'size',
    'is_inline', 'isInline', 'content_id', 'contentId'
)


def normalize_retained_mail_attachment_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, dict):
        normalized_dict = {}
        for key, item in value.items():
            normalized = normalize_retained_mail_attachment_value(item)
            if normalized is not None:
                normalized_dict[str(key)] = normalized
        return normalized_dict
    if isinstance(value, (list, tuple, set)):
        normalized_list = []
        for item in value:
            normalized = normalize_retained_mail_attachment_value(item)
            if normalized is not None:
                normalized_list.append(normalized)
        return normalized_list
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def normalize_retained_mail_attachment_metadata(attachments: Any) -> List[Dict[str, Any]]:
    if not isinstance(attachments, list):
        return []

    metadata = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        item = {}
        for key in RETAINED_MAIL_ATTACHMENT_METADATA_KEYS:
            if key not in attachment:
                continue
            normalized = normalize_retained_mail_attachment_value(attachment.get(key))
            if normalized is not None:
                item[key] = normalized
        if 'content_type' not in item and item.get('contentType'):
            item['content_type'] = item['contentType']
        if 'is_inline' not in item and 'isInline' in item:
            item['is_inline'] = bool(item.get('isInline'))
        if 'content_id' not in item and item.get('contentId'):
            item['content_id'] = item['contentId']
        metadata.append(item)
    return metadata


def find_existing_retained_normal_mail_key(account_id: int, folder: str,
                                           provider_message_ids: List[str],
                                           preferred_id_modes: List[str], db=None) -> Optional[Dict[str, str]]:
    normalized_ids = []
    for value in provider_message_ids:
        message_id = str(value or '').strip()
        if message_id and message_id not in normalized_ids:
            normalized_ids.append(message_id)
    if not normalized_ids:
        return None

    database = db or get_db()
    placeholders = ','.join('?' for _ in normalized_ids)
    rows = database.execute(
        f'''
        SELECT provider_message_id, id_mode
        FROM retained_normal_mail_messages
        WHERE account_id = ? AND folder = ? AND provider_message_id IN ({placeholders})
        ''',
        [account_id, folder, *normalized_ids]
    ).fetchall()
    if not rows:
        return None

    preferred = [str(mode or '').strip().lower() for mode in preferred_id_modes]

    def sort_key(row) -> tuple:
        row_message_id = str(row['provider_message_id'] or '')
        row_id_mode = str(row['id_mode'] or '').strip().lower()
        try:
            message_index = normalized_ids.index(row_message_id)
        except ValueError:
            message_index = len(normalized_ids)
        try:
            mode_index = preferred.index(row_id_mode)
        except ValueError:
            mode_index = len(preferred)
        return message_index, mode_index

    selected = sorted(rows, key=sort_key)[0]
    return {
        'provider_message_id': str(selected['provider_message_id'] or ''),
        'id_mode': str(selected['id_mode'] or '').strip().lower(),
    }


def normalize_retained_detail_id_mode(method: str, detail_source: str,
                                      explicit_id_mode: str = '') -> str:
    query_id_mode = str(explicit_id_mode or '').strip().lower()
    if query_id_mode:
        return query_id_mode

    normalized_source = str(detail_source or '').strip().lower()
    if normalized_source == 'graph':
        return 'graph'
    return 'uid'


def retained_detail_preferred_id_modes(primary_id_mode: str, detail_source: str) -> List[str]:
    preferred = [primary_id_mode]
    if str(detail_source or '').strip().lower() != 'graph':
        preferred.extend(['uid', 'sequence', ''])
    preferred.extend(['graph', 'uid', 'sequence', ''])
    return list(dict.fromkeys(preferred))


def resolve_retained_normal_mail_detail_key(account_id: int, folder: str,
                                            request_message_id: str, detail: Dict[str, Any],
                                            id_mode: str, detail_source: str,
                                            db=None) -> Optional[Dict[str, str]]:
    detail_id = str((detail or {}).get('id') or '').strip()
    request_id = str(request_message_id or '').strip()
    provider_message_id = detail_id or request_id
    if not provider_message_id:
        return None

    existing_key = find_existing_retained_normal_mail_key(
        account_id,
        folder,
        [provider_message_id, request_id],
        retained_detail_preferred_id_modes(id_mode, detail_source),
        db=db,
    )
    return existing_key or {
        'provider_message_id': provider_message_id,
        'id_mode': id_mode,
    }


def build_retained_normal_mail_detail_row(account: Dict[str, Any], folder: str,
                                          request_message_id: str, detail: Dict[str, Any],
                                          id_mode: str, detail_source: str,
                                          db=None) -> Optional[Dict[str, Any]]:
    account_id = int((account or {}).get('id') or 0)
    if not account_id or not detail:
        return None

    storage_folder = normalize_folder_name(folder)
    key = resolve_retained_normal_mail_detail_key(
        account_id, storage_folder, request_message_id, detail, id_mode, detail_source, db=db
    )
    if not key:
        return None

    attachments = normalize_retained_mail_attachment_metadata(detail.get('attachments'))
    return {
        'account_id': account_id,
        'folder': storage_folder,
        'provider_message_id': key['provider_message_id'],
        'id_mode': key['id_mode'],
        'subject': coerce_retained_mail_text(detail.get('subject')) or '无主题',
        'sender': coerce_retained_mail_text(detail.get('from')) or '未知',
        'recipients': coerce_retained_mail_text(detail.get('to')),
        'cc': coerce_retained_mail_text(detail.get('cc')),
        'received_at': coerce_retained_mail_text(detail.get('date')),
        'received_at_sort': retained_mail_received_at_sort(detail.get('date')),
        'body': coerce_retained_mail_text(detail.get('body')),
        'body_type': coerce_retained_mail_text(detail.get('body_type')) or 'text',
        'attachments_json': json.dumps(attachments, ensure_ascii=False),
        'has_attachments': 1 if attachments or coerce_retained_mail_bool(detail.get('has_attachments')) else 0,
    }


def upsert_retained_normal_mail_detail(account: Dict[str, Any], folder: str,
                                       request_message_id: str, detail: Dict[str, Any],
                                       method: str, detail_source: str,
                                       id_mode: str = '', db=None) -> bool:
    database = db or get_db()
    normalized_id_mode = normalize_retained_detail_id_mode(method, detail_source, id_mode)
    row = build_retained_normal_mail_detail_row(
        account, folder, request_message_id, detail, normalized_id_mode, detail_source, db=database
    )
    if not row:
        return False

    database.execute(
        '''
        INSERT INTO retained_normal_mail_messages (
            account_id, folder, provider_message_id, id_mode,
            subject, sender, recipients, cc, received_at, received_at_sort,
            has_attachments, body, body_type, attachments_json,
            body_cached, body_cached_at, last_synced_at, updated_at
        )
        VALUES (
            :account_id, :folder, :provider_message_id, :id_mode,
            :subject, :sender, :recipients, :cc, :received_at, :received_at_sort,
            :has_attachments, :body, :body_type, :attachments_json,
            1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT(account_id, folder, provider_message_id, id_mode)
        DO UPDATE SET
            subject = excluded.subject,
            sender = excluded.sender,
            recipients = excluded.recipients,
            cc = excluded.cc,
            received_at = excluded.received_at,
            received_at_sort = excluded.received_at_sort,
            has_attachments = excluded.has_attachments,
            body = excluded.body,
            body_type = excluded.body_type,
            attachments_json = excluded.attachments_json,
            body_cached = 1,
            body_cached_at = CURRENT_TIMESTAMP,
            last_synced_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        ''',
        row
    )
    database.commit()
    return True


def format_graph_email_detail(detail: Dict[str, Any], attachments: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        'id': detail.get('id'),
        'subject': detail.get('subject', '无主题'),
        'from': detail.get('from', {}).get('emailAddress', {}).get('address', '未知'),
        'to': ', '.join([
            r.get('emailAddress', {}).get('address', '')
            for r in detail.get('toRecipients', [])
            if r.get('emailAddress', {}).get('address', '')
        ]),
        'cc': ', '.join([
            r.get('emailAddress', {}).get('address', '')
            for r in detail.get('ccRecipients', [])
            if r.get('emailAddress', {}).get('address', '')
        ]),
        'date': detail.get('receivedDateTime', ''),
        'body': detail.get('body', {}).get('content', ''),
        'body_type': detail.get('body', {}).get('contentType', 'text'),
        'attachments': attachments,
    }

def build_retained_detail_success_response(account: Dict[str, Any], folder: str,
                                           message_id: str, email_detail: Dict[str, Any],
                                           method: str, detail_source: str,
                                           id_mode: str = '') -> Dict[str, Any]:
    if is_normal_mail_local_retention_enabled():
        upsert_retained_normal_mail_detail(
            account, folder, message_id, email_detail, method, detail_source, id_mode
        )
    return {'success': True, 'email': email_detail}


def fetch_imap_account_detail_response(account: Dict[str, Any], folder: str,
                                       message_id: str, method: str,
                                       id_mode: str, proxy_url: str) -> Dict[str, Any]:
    detail_result = get_email_detail_imap_generic_result(
        account['email'],
        account.get('imap_password', ''),
        account.get('imap_host', ''),
        account.get('imap_port', 993),
        message_id,
        folder,
        account.get('provider', 'custom'),
        proxy_url
    )
    if detail_result.get('success'):
        return build_retained_detail_success_response(
            account, folder, message_id, detail_result.get('email', {}), method, 'imap', id_mode
        )
    return {'success': False, 'error': detail_result.get('error', '获取邮件详情失败')}


def fetch_graph_detail_response(account: Dict[str, Any], folder: str,
                                message_id: str, method: str, id_mode: str,
                                proxy_url: str, fallback_proxy_urls: List[str]) -> Optional[Dict[str, Any]]:
    detail = get_email_detail_graph(
        account['client_id'], account['refresh_token'], message_id, proxy_url, fallback_proxy_urls
    )
    if not detail:
        return None

    attachments = []
    if detail.get('hasAttachments'):
        attachments = get_email_attachments_graph(
            account['client_id'], account['refresh_token'], message_id, proxy_url, fallback_proxy_urls
        ) or []
    email_detail = format_graph_email_detail(detail, attachments)
    return build_retained_detail_success_response(
        account, folder, message_id, email_detail, method, 'graph', id_mode
    )


def fetch_oauth_imap_detail_response(account: Dict[str, Any], folder: str,
                                     message_id: str, method: str, id_mode: str,
                                     proxy_url: str, fallback_proxy_urls: List[str]) -> Optional[Dict[str, Any]]:
    requested_mode = str(id_mode or '').strip().lower()
    preferred_id_mode = requested_mode if requested_mode in {'uid', 'sequence'} else 'uid'
    detail = get_email_detail_imap(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        message_id,
        folder,
        proxy_url,
        fallback_proxy_urls,
        preferred_id_mode,
    )
    if not detail:
        return None
    return build_retained_detail_success_response(
        account, folder, message_id, detail, method, 'imap', id_mode
    )


def parse_non_negative_int(raw_value: Any, default: int, max_value: Optional[int] = None) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default
    value = max(0, value)
    return min(value, max_value) if max_value is not None else value


def is_local_retention_request() -> bool:
    source = str(request.args.get('source', '') or '').strip().lower()
    local_only = str(request.args.get('local_only', '') or '').strip().lower()
    return source == 'local' or local_only in {'1', 'true', 'yes', 'on'}


def is_prefer_local_detail_request() -> bool:
    source = str(request.args.get('source', '') or '').strip().lower()
    prefer_local = str(request.args.get('prefer_local', '') or '').strip().lower()
    return source == 'local' or prefer_local in {'1', 'true', 'yes', 'on'}


def retained_mail_row_to_list_item(row) -> Dict[str, Any]:
    item = {
        'id': row['provider_message_id'],
        'subject': row['subject'] or '无主题',
        'from': row['sender'] or '未知',
        'to': row['recipients'] or '',
        'date': row['received_at'] or '',
        'is_read': bool(row['is_read']),
        'has_attachments': bool(row['has_attachments']),
        'body_preview': row['body_preview'] or '',
        'folder': row['folder'] or 'inbox',
        'id_mode': row['id_mode'] or '',
    }
    if 'body' in row.keys() and row['body']:
        item['body'] = row['body']
    return item


def parse_retained_mail_attachments(raw_attachments: Any) -> List[Dict[str, Any]]:
    if not raw_attachments:
        return []
    try:
        attachments = json.loads(raw_attachments)
    except (TypeError, ValueError):
        return []
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


def retained_mail_row_to_detail_response(row) -> Dict[str, Any]:
    attachments = parse_retained_mail_attachments(row['attachments_json'])
    return {
        'success': True,
        'email': {
            'id': row['provider_message_id'],
            'subject': row['subject'] or '无主题',
            'from': row['sender'] or '未知',
            'to': row['recipients'] or '',
            'cc': row['cc'] or '',
            'date': row['received_at'] or '',
            'body': row['body'] or '',
            'body_type': row['body_type'] or 'text',
            'attachments': attachments,
            'has_attachments': bool(row['has_attachments']),
            'folder': row['folder'] or 'inbox',
            'id_mode': row['id_mode'] or '',
        },
        'method': 'Local Retention',
        'source': 'local_retention',
        'request_method': 'local',
        'local_retention': True,
    }


def fetch_retained_normal_mail_detail(account: Dict[str, Any], folder: str,
                                      message_id: str, id_mode: str = '') -> Optional[Dict[str, Any]]:
    account_id = int((account or {}).get('id') or 0)
    provider_message_id = str(message_id or '').strip()
    if not account_id or not provider_message_id:
        return None

    folder_name = normalize_folder_name(folder)
    requested_id_mode = str(id_mode or '').strip().lower()
    params: List[Any] = [account_id, provider_message_id]
    folder_filter = ''
    id_mode_filter = ''
    if folder_name != 'all':
        folder_filter = 'AND folder = ?'
        params.append(folder_name)
    if requested_id_mode:
        id_mode_filter = 'AND id_mode = ?'
        params.append(requested_id_mode)

    row = get_db().execute(
        f'''
        SELECT provider_message_id, id_mode, folder, subject, sender,
               recipients, cc, received_at, body, body_type,
               attachments_json, has_attachments
        FROM retained_normal_mail_messages
        WHERE account_id = ?
          AND provider_message_id = ?
          AND body_cached = 1
          {folder_filter}
          {id_mode_filter}
        ORDER BY COALESCE(body_cached_at, updated_at, created_at) DESC, id DESC
        LIMIT 1
        ''',
        params
    ).fetchone()
    if not row:
        return None
    return retained_mail_row_to_detail_response(row)


def normalize_body_retention_items(raw_items: Any, fallback_folder: str,
                                   fallback_method: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for raw_item in raw_items or []:
        if isinstance(raw_item, dict):
            message_id = str(raw_item.get('id') or raw_item.get('message_id') or '').strip()
            folder = normalize_folder_name(raw_item.get('folder', fallback_folder))
            id_mode = str(raw_item.get('id_mode') or '').strip().lower()
            method = str(raw_item.get('method') or fallback_method or 'graph').strip().lower()
        else:
            message_id = str(raw_item or '').strip()
            folder = normalize_folder_name(fallback_folder)
            id_mode = ''
            method = str(fallback_method or 'graph').strip().lower()
        if message_id:
            items.append({'id': message_id, 'folder': folder, 'id_mode': id_mode, 'method': method})
    return items


def find_retained_body_cache_row(account_id: int, item: Dict[str, str], db=None):
    message_id = str(item.get('id') or '').strip()
    if not account_id or not message_id:
        return None

    folder = normalize_folder_name(item.get('folder', 'inbox'))
    id_mode = str(item.get('id_mode') or '').strip().lower()
    filters = ['account_id = ?', 'provider_message_id = ?']
    params: List[Any] = [account_id, message_id]
    if folder != 'all':
        filters.append('folder = ?')
        params.append(folder)
    if id_mode:
        filters.append('id_mode = ?')
        params.append(id_mode)
    return (db or get_db()).execute(
        f'''
        SELECT folder, provider_message_id, id_mode, body_cached
        FROM retained_normal_mail_messages
        WHERE {' AND '.join(filters)}
        ORDER BY body_cached DESC, COALESCE(updated_at, created_at) DESC, id DESC
        LIMIT 1
        ''',
        params
    ).fetchone()


def retained_body_fetch_item(item: Dict[str, str], cache_row) -> Dict[str, str]:
    if not cache_row:
        return item
    fetch_item = dict(item)
    fetch_item['folder'] = str(cache_row['folder'] or fetch_item.get('folder') or 'inbox')
    fetch_item['id_mode'] = str(cache_row['id_mode'] or fetch_item.get('id_mode') or '').strip().lower()
    return fetch_item


def retained_body_fetch_method(account: Dict[str, Any], item: Dict[str, str]) -> str:
    if account.get('account_type') == 'imap':
        return 'imap'
    id_mode = str(item.get('id_mode') or '').strip().lower()
    if id_mode == 'graph':
        return 'graph'
    if id_mode in {'uid', 'sequence'}:
        return 'imap'
    method = str(item.get('method') or 'graph').strip().lower()
    return 'graph' if method == 'graph' else 'imap'


def fetch_retained_body_response(account: Dict[str, Any], item: Dict[str, str],
                                 proxy_url: str, fallback_proxy_urls: List[str]) -> Dict[str, Any]:
    method = retained_body_fetch_method(account, item)
    message_id = item['id']
    folder = normalize_folder_name(item.get('folder', 'inbox'))
    id_mode = str(item.get('id_mode') or '').strip().lower()
    if account.get('account_type') == 'imap':
        return fetch_imap_account_detail_response(account, folder, message_id, method, id_mode, proxy_url)
    if method == 'graph':
        result = fetch_graph_detail_response(
            account, folder, message_id, method, id_mode, proxy_url, fallback_proxy_urls
        )
    else:
        result = fetch_oauth_imap_detail_response(
            account, folder, message_id, method, id_mode, proxy_url, fallback_proxy_urls
        )
    return result or {'success': False, 'error': '获取邮件详情失败'}


def retain_normal_mail_bodies(account: Dict[str, Any], items: List[Dict[str, str]]) -> Dict[str, Any]:
    db = get_db()
    account_id = int(account.get('id') or 0)
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    cached_count = skipped_count = failed_count = fetched_count = 0
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for item in items:
        cache_row = find_retained_body_cache_row(account_id, item, db=db)
        if cache_row and int(cache_row['body_cached'] or 0) == 1:
            skipped_count += 1
            results.append({'id': item['id'], 'status': 'skipped', 'reason': 'body_cached'})
            continue
        if fetched_count >= RETAINED_MAIL_BODY_FETCH_LIMIT:
            skipped_count += 1
            results.append({'id': item['id'], 'status': 'skipped', 'reason': 'fetch_limit'})
            continue
        fetch_item = retained_body_fetch_item(item, cache_row)
        fetched_count += 1
        try:
            response = fetch_retained_body_response(account, fetch_item, proxy_url, fallback_proxy_urls)
        except Exception as exc:
            response = {'success': False, 'error': str(exc)}
        if response.get('success'):
            cached_count += 1
            results.append({'id': item['id'], 'status': 'cached'})
            continue
        failed_count += 1
        error = str(response.get('error') or '获取邮件详情失败')
        errors.append({'id': item['id'], 'error': error})
        results.append({'id': item['id'], 'status': 'failed', 'error': error})

    return {
        'success': failed_count == 0,
        'cached_count': cached_count,
        'skipped_count': skipped_count,
        'failed_count': failed_count,
        'limit': RETAINED_MAIL_BODY_FETCH_LIMIT,
        'results': results,
        'errors': errors,
    }


def email_matches_local_retention_filters(item: Dict[str, Any], subject_contains: str = '',
                                          from_contains: str = '', keyword: str = '') -> bool:
    subject = str(item.get('subject', '') or '')
    sender = str(item.get('from', '') or '')
    preview = str(item.get('body_preview', '') or '')
    body = strip_html_content(str(item.get('body', '') or ''))
    if subject_contains and subject_contains not in subject.lower():
        return False
    if from_contains and from_contains not in sender.lower():
        return False
    if not keyword:
        return True
    return keyword in '\n'.join([subject, preview, body]).lower()


def retained_mail_like_param(value: str) -> str:
    return f'%{escape_sql_like_literal(str(value or "").lower())}%'


def register_retained_mail_sql_functions(db) -> None:
    db.create_function(
        'retained_mail_strip_html',
        1,
        lambda value: strip_html_content(str(value or ''))
    )


def build_retained_mail_filter_sql(subject_contains: str = '',
                                   from_contains: str = '',
                                   keyword: str = '') -> tuple[str, List[Any]]:
    clauses = []
    params: List[Any] = []
    if subject_contains:
        clauses.append("LOWER(COALESCE(subject, '')) LIKE ? ESCAPE '\\'")
        params.append(retained_mail_like_param(subject_contains))
    if from_contains:
        clauses.append("LOWER(COALESCE(sender, '')) LIKE ? ESCAPE '\\'")
        params.append(retained_mail_like_param(from_contains))
    if keyword:
        keyword_param = retained_mail_like_param(keyword)
        clauses.append(
            """(
                LOWER(COALESCE(subject, '')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(body_preview, '')) LIKE ? ESCAPE '\\'
                OR LOWER(retained_mail_strip_html(COALESCE(body, ''))) LIKE ? ESCAPE '\\'
            )"""
        )
        params.extend([keyword_param, keyword_param, keyword_param])
    if not clauses:
        return '', []
    return 'AND ' + ' AND '.join(clauses), params


def fetch_retained_normal_mail_list(account: Dict[str, Any], folder: str,
                                    skip: int, top: int,
                                    include_body: bool = False,
                                    subject_contains: str = '',
                                    from_contains: str = '',
                                    keyword: str = '') -> Dict[str, Any]:
    folder_name = normalize_folder_name(folder)
    if folder_name not in VALID_MAIL_FOLDERS:
        return {
            'success': False,
            'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS))}'
        }

    params: List[Any] = [int(account['id'])]
    folder_filter = ''
    if folder_name != 'all':
        folder_filter = 'AND folder = ?'
        params.append(folder_name)

    dedupe_partition = 'account_id, folder, provider_message_id'
    dedupe_order = '''
        CASE WHEN body_cached = 1 THEN 0 ELSE 1 END,
        received_at_sort DESC,
        updated_at DESC,
        id DESC
    '''
    db = get_db()
    if keyword:
        register_retained_mail_sql_functions(db)
    filter_sql, filter_params = build_retained_mail_filter_sql(
        subject_contains, from_contains, keyword
    )
    cte_sql = f'''
        WITH ranked_retained AS (
            SELECT provider_message_id, subject, sender, recipients, received_at,
                   is_read, has_attachments, body_preview, body, folder, id_mode,
                   received_at_sort, id,
                   ROW_NUMBER() OVER (
                       PARTITION BY {dedupe_partition}
                       ORDER BY {dedupe_order}
                   ) AS retained_rank
            FROM retained_normal_mail_messages
            WHERE account_id = ? AND list_cached = 1 {folder_filter}
        ),
        filtered_retained AS (
            SELECT *
            FROM ranked_retained
            WHERE retained_rank = 1 {filter_sql}
        )
    '''
    total_row = db.execute(
        cte_sql + '''
        SELECT COUNT(*) AS count
        FROM filtered_retained
        ''',
        params + filter_params
    ).fetchone()
    total_count = int(total_row['count'] if total_row else 0)

    body_column = ', body' if include_body else ''
    rows = db.execute(
        cte_sql + f'''
        SELECT provider_message_id, subject, sender, recipients, received_at,
               is_read, has_attachments, body_preview{body_column}, folder, id_mode
        FROM filtered_retained
        ORDER BY received_at_sort DESC, id DESC
        LIMIT ? OFFSET ?
        ''',
        params + filter_params + [top + 1, skip]
    ).fetchall()

    emails = [retained_mail_row_to_list_item(row) for row in rows[:top]]
    return {
        'success': True,
        'emails': emails,
        'has_more': len(rows) > top or total_count > skip + len(emails),
        'count': total_count,
        'method': 'Local Retention',
        'source': 'local_retention',
        'request_method': 'local',
        'local_retention': True,
        'folder': folder_name,
    }


def format_graph_email_item(item: Dict[str, Any], folder: str) -> Dict[str, Any]:
    return normalize_email_list_item({
        'id': item.get('id'),
        'subject': item.get('subject', '无主题'),
        'from': item.get('from', {}).get('emailAddress', {}).get('address', '未知'),
        'to': ', '.join([
            recipient.get('emailAddress', {}).get('address', '')
            for recipient in (item.get('toRecipients') or [])
            if recipient.get('emailAddress', {}).get('address', '')
        ]),
        'date': item.get('receivedDateTime', ''),
        'is_read': item.get('isRead', False),
        'has_attachments': item.get('hasAttachments', False),
        'body_preview': item.get('bodyPreview', ''),
        'folder': folder,
        'id_mode': 'graph',
    }, folder)


def format_email_items(items: List[Dict[str, Any]], folder: str) -> List[Dict[str, Any]]:
    return [normalize_email_list_item(item, folder) for item in items]


def is_transport_error_payload(error_payload: Any) -> bool:
    if not isinstance(error_payload, dict):
        return False
    error_type = str(error_payload.get('type') or '').strip()
    return error_type in {
        'ProxyError',
        'ConnectionError',
        'ConnectTimeout',
        'ReadTimeout',
        'Timeout',
    }


def merge_folder_results(results: Dict[str, Dict[str, Any]], skip: int, top: int) -> Dict[str, Any]:
    successful = {folder: result for folder, result in results.items() if result.get('success')}
    if not successful:
        details = {folder: result.get('error') for folder, result in results.items()}
        return {
            'success': False,
            'error': '无法获取邮件，所有方式均失败',
            'details': details
        }

    merged = []
    methods = []
    has_more = False
    partial_errors = {}
    folder_summaries = {}
    for folder, result in results.items():
        folder_summary = {
            'success': bool(result.get('success')),
            'fetched_count': len(result.get('emails', [])) if result.get('success') else 0,
            'has_more': bool(result.get('has_more')) if result.get('success') else False,
        }
        request_method = str(result.get('request_method') or '').strip().lower()
        if request_method in {'graph', 'imap'}:
            folder_summary['request_method'] = request_method
        if result.get('method'):
            folder_summary['method'] = result['method']
        if not result.get('success') and result.get('error') is not None:
            folder_summary['error'] = result.get('error')
        folder_summaries[folder] = folder_summary

        if result.get('success'):
            merged.extend(result.get('emails', []))
            if result.get('method'):
                methods.append(result['method'])
            has_more = has_more or bool(result.get('has_more'))
        else:
            partial_errors[folder] = result.get('error')

    merged.sort(key=lambda item: parse_email_datetime(item.get('date')) or datetime.min, reverse=True)
    sliced = merged[skip:skip + top]

    unique_methods = []
    for method in methods:
        if method and method not in unique_methods:
            unique_methods.append(method)

    response = {
        'success': True,
        'emails': sliced,
        'method': ' / '.join(unique_methods) if unique_methods else '',
        'has_more': has_more or len(merged) > skip + top,
        'folder_summaries': folder_summaries,
    }
    if partial_errors:
        response['partial'] = True
        response['details'] = partial_errors
    return response


def fetch_account_folder_emails(account: Dict[str, Any], folder: str, skip: int, top: int,
                                proxy_url: str = '', fallback_proxy_urls: List[str] = None) -> Dict[str, Any]:
    folder_name = normalize_folder_name(folder)
    if folder_name not in VALID_MAIL_FOLDERS or folder_name == 'all':
        return {
            'success': False,
            'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS - {"all"} | {"all"}))}'
        }

    if account.get('account_type') == 'imap':
        result = get_emails_imap_generic(
            account['email'],
            account.get('imap_password', ''),
            account.get('imap_host', ''),
            account.get('imap_port', 993),
            folder_name,
            account.get('provider', 'custom'),
            skip,
            top,
            proxy_url
        )
        if result.get('success'):
            return {
                'success': True,
                'emails': format_email_items(result.get('emails', []), folder_name),
                'method': result.get('method', 'IMAP (Generic)'),
                'has_more': bool(result.get('has_more')),
                'request_method': 'imap',
            }
        return {
            'success': False,
            'error': result.get('error', '获取邮件失败'),
            'details': {'imap_generic': result.get('error')}
        }

    all_errors = {}
    graph_result = get_emails_graph(
        account['client_id'],
        account['refresh_token'],
        folder_name,
        skip,
        top,
        proxy_url,
        fallback_proxy_urls,
    )
    if graph_result.get('success'):
        return {
            'success': True,
            'emails': [format_graph_email_item(item, folder_name) for item in graph_result.get('emails', [])],
            'method': 'Graph API',
            'has_more': len(graph_result.get('emails', [])) >= top,
            'request_method': 'graph',
        }

    graph_error = graph_result.get('error')
    all_errors['graph'] = graph_error
    if is_transport_error_payload(graph_error):
        connection_error_message = (
            '代理连接失败或请求超时，请检查账号代理或分组代理设置'
            if proxy_url
            else '连接 Microsoft 服务失败或超时，请检查服务器网络、DNS 或上游访问能力'
        )
        return {
            'success': False,
            'error': connection_error_message,
            'details': all_errors
        }

    imap_new_result = get_emails_imap_with_server(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        folder_name,
        skip,
        top,
        IMAP_SERVER_NEW,
        proxy_url,
        fallback_proxy_urls,
    )
    if imap_new_result.get('success'):
        return {
            'success': True,
            'emails': format_email_items(imap_new_result.get('emails', []), folder_name),
            'method': 'IMAP (New)',
            'has_more': bool(imap_new_result.get('has_more')),
            'request_method': 'imap',
        }
    all_errors['imap_new'] = imap_new_result.get('error')

    imap_old_result = get_emails_imap_with_server(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        folder_name,
        skip,
        top,
        IMAP_SERVER_OLD,
        proxy_url,
        fallback_proxy_urls,
    )
    if imap_old_result.get('success'):
        return {
            'success': True,
            'emails': format_email_items(imap_old_result.get('emails', []), folder_name),
            'method': 'IMAP (Old)',
            'has_more': bool(imap_old_result.get('has_more')),
            'request_method': 'imap',
        }
    all_errors['imap_old'] = imap_old_result.get('error')

    return {
        'success': False,
        'error': '无法获取邮件，所有方式均失败',
        'details': all_errors
    }


def fetch_account_emails_with_proxy(account: Dict[str, Any], folder: str, skip: int, top: int,
                                    proxy_url: str = '', fallback_proxy_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    folder_name = normalize_folder_name(folder)
    if folder_name not in VALID_MAIL_FOLDERS:
        return {
            'success': False,
            'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS))}'
        }

    if folder_name == 'all':
        merged_top = max(1, min(100, top * 2))
        folder_jobs = ('inbox', 'junkemail')
        results = {}
        executor = ThreadPoolExecutor(max_workers=len(folder_jobs), thread_name_prefix='mail-folder-fetch')
        future_map = {
            folder_job: executor.submit(
                fetch_account_folder_emails,
                account,
                folder_job,
                skip,
                top,
                proxy_url,
                fallback_proxy_urls,
            )
            for folder_job in folder_jobs
        }
        try:
            done, not_done = wait(future_map.values(), timeout=MAIL_FETCH_OVERALL_TIMEOUT)
            for folder_job, future in future_map.items():
                if future in done:
                    try:
                        results[folder_job] = future.result()
                    except Exception as exc:
                        results[folder_job] = {
                            'success': False,
                            'error': build_error_payload(
                                'EMAIL_FETCH_FAILED',
                                '获取邮件失败，请检查账号配置',
                                type(exc).__name__,
                                500,
                                str(exc)
                            )
                        }
                    continue

                future.cancel()
                results[folder_job] = {
                    'success': False,
                    'error': build_error_payload(
                        'EMAIL_FETCH_TIMEOUT',
                        '获取邮件超时，请稍后重试',
                        'TimeoutError',
                        504,
                        f'folder={folder_job}, timeout={MAIL_FETCH_OVERALL_TIMEOUT}s'
                    )
                }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return merge_folder_results(
            results,
            0,
            merged_top
        )

    return fetch_account_folder_emails(account, folder_name, skip, top, proxy_url, fallback_proxy_urls)


def fetch_account_emails(account: Dict[str, Any], folder: str, skip: int, top: int) -> Dict[str, Any]:
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    return fetch_account_emails_with_proxy(account, folder, skip, top, proxy_url, fallback_proxy_urls)


DISABLED_ACCOUNT_CHECK_MAX_EMAILS = 200
DISABLED_ACCOUNT_CHECK_RECENT_COUNT = 10
DISABLED_ACCOUNT_CHECK_PATTERNS = [
    re.compile(
        r'your\s+account\s+has\s+been\s+banned\s+because\s+recent\s+activity\s+violated\s+our\s+terms\s+and\s+usage\s+policies',
        re.IGNORECASE,
    ),
    re.compile(r'account\s+has\s+been\s+banned.*violat(?:ed|es|ing).*terms.*usage\s+polic', re.IGNORECASE | re.DOTALL),
    re.compile(r'account\s+(?:has\s+been\s+)?(?:disabled|deactivated|suspended).*violat(?:ed|es|ing).*terms', re.IGNORECASE | re.DOTALL),
    re.compile(r'openai\s*-\s*access\s+deactivated', re.IGNORECASE),
]
DISABLED_ACCOUNT_CHECK_MATCH_RULES = [
    {
        'code': 'openai_trust_and_safety_sender',
        'label': 'OpenAI Trust & Safety 发件人',
        'description': '邮件发件人包含 trustandsafety@tm.openai.com。',
    },
    {
        'code': 'openai_access_deactivated_subject',
        'label': 'OpenAI Access Deactivated 标题',
        'description': '邮件标题包含 OpenAI - Access Deactivated，通常后跟 [C-...] 编号。',
    },
    {
        'code': 'openai_banned_terms_body',
        'label': '账号违反 Terms/Usage Policies 正文',
        'description': '邮件正文包含 Your account has been banned because recent activity violated our Terms and Usage Policies。',
    },
    {
        'code': 'disabled_terms_notice',
        'label': '停用/封禁条款通知',
        'description': '邮件文本包含账号被 disabled、deactivated、suspended 或 banned 且与 terms/usage policies 相关的通知。',
    },
]
DISABLED_ACCOUNT_CHECK_RULE_LABELS = {
    rule['code']: rule['label']
    for rule in DISABLED_ACCOUNT_CHECK_MATCH_RULES
}
DISABLED_GROUP_CHECK_TASKS: Dict[str, Dict[str, Any]] = {}
DISABLED_GROUP_CHECK_TASK_LOCK = threading.Lock()
DISABLED_GROUP_CHECK_TASK_MAX_AGE_SECONDS = 3600


def get_disabled_group_check_max_workers() -> int:
    try:
        return max(1, int(os.getenv('DISABLED_GROUP_CHECK_MAX_WORKERS', '6') or 6))
    except (TypeError, ValueError):
        return 6


DISABLED_GROUP_CHECK_MAX_WORKERS = get_disabled_group_check_max_workers()


def normalize_disabled_check_recent_count(value: Any) -> int:
    try:
        recent_count = int(value or DISABLED_ACCOUNT_CHECK_RECENT_COUNT)
    except (TypeError, ValueError):
        recent_count = DISABLED_ACCOUNT_CHECK_RECENT_COUNT
    return max(1, min(10, recent_count))


def dump_disabled_check_json(value: Any) -> str:
    if value in (None, ''):
        return ''
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return ''


def load_disabled_check_json(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def serialize_disabled_group_check_task_row(row: Any, include_payload: bool = False) -> Dict[str, Any]:
    task = dict(row)
    summary = load_disabled_check_json(task.pop('summary_json', '')) or {}
    status_update = load_disabled_check_json(task.pop('status_update_json', '')) or {}
    payload = load_disabled_check_json(task.pop('payload_json', '')) if include_payload else None
    result = {
        'task_id': task.get('task_id', ''),
        'group_id': task.get('group_id'),
        'group_name': task.get('group_name', ''),
        'status': task.get('status', ''),
        'task_status': task.get('status', ''),
        'recent_count': int(task.get('recent_count') or 0),
        'checked_count': int(task.get('checked_count') or 0),
        'disabled_count': int(task.get('disabled_count') or 0),
        'marked_inactive_count': int(task.get('marked_inactive_count') or 0),
        'error_count': int(task.get('error_count') or 0),
        'error': task.get('error_message', '') or '',
        'summary': summary,
        'status_update': status_update,
        'created_at': task.get('created_at'),
        'started_at': task.get('started_at'),
        'completed_at': task.get('completed_at'),
        'updated_at': task.get('updated_at'),
    }
    if include_payload:
        result['payload'] = payload
    return result


def create_disabled_group_check_task_record(task_id: str, group_id: int, group_name: str, recent_count: int,
                                            created_at: str) -> None:
    db = get_db()
    db.execute(
        '''
        INSERT OR REPLACE INTO group_disabled_check_tasks
            (task_id, group_id, group_name, status, recent_count, created_at, updated_at)
        VALUES (?, ?, ?, 'running', ?, ?, ?)
        ''',
        (task_id, group_id, group_name or '', recent_count, created_at, created_at)
    )
    db.commit()


def mark_disabled_group_check_task_started(task_id: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        '''
        UPDATE group_disabled_check_tasks
        SET started_at = COALESCE(started_at, ?), updated_at = ?
        WHERE task_id = ?
        ''',
        (now_iso, now_iso, task_id)
    )
    db.commit()


def persist_disabled_group_check_task_completion(task_id: str, payload: Dict[str, Any]) -> None:
    summary = payload.get('summary') or {}
    status_update = payload.get('status_update') or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        '''
        UPDATE group_disabled_check_tasks
        SET status = 'completed',
            checked_count = ?,
            disabled_count = ?,
            marked_inactive_count = ?,
            error_count = ?,
            error_message = '',
            summary_json = ?,
            status_update_json = ?,
            payload_json = ?,
            completed_at = ?,
            updated_at = ?
        WHERE task_id = ?
        ''',
        (
            int(summary.get('checked_count') or 0),
            int(summary.get('disabled_count') or 0),
            int(summary.get('marked_inactive_count') or 0),
            int(summary.get('error_count') or 0),
            dump_disabled_check_json(summary),
            dump_disabled_check_json(status_update),
            dump_disabled_check_json(payload),
            now_iso,
            now_iso,
            task_id,
        )
    )
    db.commit()


def persist_disabled_group_check_task_failure(task_id: str, error_message: str,
                                              payload: Optional[Dict[str, Any]] = None) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    safe_payload = payload or {}
    db = get_db()
    db.execute(
        '''
        UPDATE group_disabled_check_tasks
        SET status = 'failed',
            error_message = ?,
            payload_json = ?,
            completed_at = ?,
            updated_at = ?
        WHERE task_id = ?
        ''',
        (
            error_message or '当前分组停用检测失败',
            dump_disabled_check_json(safe_payload),
            now_iso,
            now_iso,
            task_id,
        )
    )
    db.commit()


def get_persisted_disabled_group_check_task(task_id: str, include_payload: bool = False) -> Optional[Dict[str, Any]]:
    db = get_db()
    row = db.execute(
        '''
        SELECT *
        FROM group_disabled_check_tasks
        WHERE task_id = ?
        ''',
        (task_id,)
    ).fetchone()
    return serialize_disabled_group_check_task_row(row, include_payload=include_payload) if row else None


def list_disabled_group_check_task_records(group_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    db = get_db()
    safe_limit = max(1, min(100, int(limit or 50)))
    if group_id:
        rows = db.execute(
            '''
            SELECT *
            FROM group_disabled_check_tasks
            WHERE group_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            ''',
            (group_id, safe_limit)
        ).fetchall()
    else:
        rows = db.execute(
            '''
            SELECT *
            FROM group_disabled_check_tasks
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            ''',
            (safe_limit,)
        ).fetchall()
    return [serialize_disabled_group_check_task_row(row) for row in rows]


def cleanup_disabled_group_check_tasks() -> None:
    now_ts = time.time()
    with DISABLED_GROUP_CHECK_TASK_LOCK:
        expired_task_ids = [
            task_id
            for task_id, task in DISABLED_GROUP_CHECK_TASKS.items()
            if now_ts - float(task.get('created_ts') or now_ts) > DISABLED_GROUP_CHECK_TASK_MAX_AGE_SECONDS
        ]
        for task_id in expired_task_ids:
            DISABLED_GROUP_CHECK_TASKS.pop(task_id, None)


def get_disabled_group_check_task(task_id: str) -> Optional[Dict[str, Any]]:
    cleanup_disabled_group_check_tasks()
    with DISABLED_GROUP_CHECK_TASK_LOCK:
        task = DISABLED_GROUP_CHECK_TASKS.get(task_id)
        return dict(task) if task else None


def update_disabled_group_check_task(task_id: str, **updates) -> None:
    with DISABLED_GROUP_CHECK_TASK_LOCK:
        task = DISABLED_GROUP_CHECK_TASKS.get(task_id)
        if not task:
            return
        task.update(updates)
        task['updated_at'] = datetime.now(timezone.utc).isoformat()


def log_disabled_group_check_event(message: str) -> None:
    print(f'[disabled-check] {message}', flush=True)


def run_group_disabled_check_task(task_id: str, group_id: int, data: Dict[str, Any]) -> None:
    with app.app_context():
        try:
            log_disabled_group_check_event(f'task started: task_id={task_id} group_id={group_id}')
            app.logger.info('group disabled-check task started: task_id=%s group_id=%s', task_id, group_id)
            mark_disabled_group_check_task_started(task_id)
            payload, error = build_group_disabled_check_payload(group_id, data)
            if error:
                error_payload, status_code = error
                error_message = error_payload.get('error') or '当前分组停用检测失败'
                update_disabled_group_check_task(
                    task_id,
                    status='failed',
                    http_status=status_code,
                    error=error_message,
                    payload=error_payload,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                persist_disabled_group_check_task_failure(task_id, error_message, error_payload)
                app.logger.warning(
                    'group disabled-check task failed: task_id=%s group_id=%s status=%s error=%s',
                    task_id,
                    group_id,
                    status_code,
                    error_payload.get('error'),
                )
                log_disabled_group_check_event(
                    f'task failed: task_id={task_id} group_id={group_id} status={status_code} error={error_payload.get("error")}'
                )
                return

            persist_disabled_group_check_task_completion(task_id, payload)
            update_disabled_group_check_task(
                task_id,
                status='completed',
                payload=payload,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            summary = payload.get('summary') or {}
            app.logger.info(
                'group disabled-check task completed: task_id=%s group_id=%s checked=%s disabled=%s marked=%s errors=%s',
                task_id,
                group_id,
                summary.get('checked_count'),
                summary.get('disabled_count'),
                summary.get('marked_inactive_count'),
                summary.get('error_count'),
            )
            log_disabled_group_check_event(
                f'task completed: task_id={task_id} group_id={group_id} checked={summary.get("checked_count")} '
                f'disabled={summary.get("disabled_count")} marked={summary.get("marked_inactive_count")} errors={summary.get("error_count")}'
            )
        except Exception as exc:
            error_message = sanitize_error_details(str(exc))
            persist_disabled_group_check_task_failure(task_id, error_message, {'success': False, 'error': error_message})
            update_disabled_group_check_task(
                task_id,
                status='failed',
                error=error_message,
                payload={'success': False, 'error': error_message},
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            app.logger.exception('group disabled-check task crashed: task_id=%s group_id=%s', task_id, group_id)
            log_disabled_group_check_event(f'task crashed: task_id={task_id} group_id={group_id} error={error_message}')


def normalize_disabled_check_email_inputs(raw_emails: Any = None, raw_text: str = '') -> List[Dict[str, Any]]:
    values: List[str] = []
    if isinstance(raw_emails, list):
        values.extend(str(value or '') for value in raw_emails)
    elif isinstance(raw_emails, str):
        values.extend(raw_emails.splitlines())

    if raw_text:
        values.extend(str(raw_text or '').splitlines())

    items = []
    for index, value in enumerate(values, start=1):
        email_addr = str(value or '').strip()
        if not email_addr:
            continue
        items.append({
            'line': index,
            'email': email_addr,
            'normalized_email': normalize_email_address(email_addr),
        })
    return items


def clean_disabled_check_text(value: Any) -> str:
    text = str(value or '')
    if not text:
        return ''
    text = strip_html_content(text)
    try:
        text = html.unescape(text)
    except Exception:
        pass
    return re.sub(r'\s+', ' ', text).strip()


def text_matches_disabled_account_notice(value: Any) -> bool:
    return disabled_match_codes_indicate_disabled(get_disabled_account_notice_match_codes(value))


def get_disabled_account_notice_match_codes(value: Any) -> List[str]:
    text = clean_disabled_check_text(value)
    if not text:
        return []

    match_codes = []
    if re.search(r'trustandsafety@tm\.openai\.com', text, re.IGNORECASE):
        match_codes.append('openai_trust_and_safety_sender')
    if re.search(r'openai\s*-\s*access\s+deactivated(?:\s*\[[^\]]+\])?', text, re.IGNORECASE):
        match_codes.append('openai_access_deactivated_subject')
    if re.search(
        r'your\s+account\s+has\s+been\s+banned\s+because\s+recent\s+activity\s+violated\s+our\s+terms\s+and\s+usage\s+policies',
        text,
        re.IGNORECASE,
    ):
        match_codes.append('openai_banned_terms_body')
    if any(pattern.search(text) for pattern in DISABLED_ACCOUNT_CHECK_PATTERNS):
        match_codes.append('disabled_terms_notice')

    return list(dict.fromkeys(match_codes))


def disabled_match_codes_indicate_disabled(match_codes: List[str]) -> bool:
    disabled_signal_codes = {
        'openai_access_deactivated_subject',
        'openai_banned_terms_body',
        'disabled_terms_notice',
    }
    return any(code in disabled_signal_codes for code in match_codes)


def stringify_disabled_check_error(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or error)
    return str(error or '')


def build_disabled_check_detail_text(account: Dict[str, Any], email_item: Dict[str, Any],
                                     proxy_url: str = '', fallback_proxy_urls: Optional[List[str]] = None) -> tuple[str, str]:
    message_id = str(email_item.get('id') or '').strip()
    folder = normalize_folder_name(email_item.get('folder') or 'inbox')
    id_mode = str(email_item.get('id_mode') or '').strip().lower()
    pieces = [
        email_item.get('subject', ''),
        email_item.get('from', ''),
        email_item.get('to', ''),
        email_item.get('body_preview', ''),
    ]

    if not message_id:
        return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), 'message_id 为空'

    try:
        if account.get('account_type') == 'imap':
            detail_result = get_email_detail_imap_generic_result(
                account['email'],
                account.get('imap_password', ''),
                account.get('imap_host', ''),
                account.get('imap_port', 993),
                message_id,
                folder,
                account.get('provider', 'custom'),
                proxy_url,
            )
            if detail_result.get('success'):
                detail = detail_result.get('email') or {}
                pieces.extend([detail.get('subject', ''), detail.get('from', ''), detail.get('to', ''), detail.get('body', '')])
            else:
                return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), stringify_disabled_check_error(detail_result.get('error'))
        elif id_mode == 'graph':
            detail = get_email_detail_graph(
                account['client_id'],
                account['refresh_token'],
                message_id,
                proxy_url,
                fallback_proxy_urls,
            )
            if detail:
                pieces.extend([
                    detail.get('subject', ''),
                    detail.get('bodyPreview', ''),
                    (detail.get('body') or {}).get('content', ''),
                ])
            else:
                return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), 'Graph 邮件详情获取失败'
        else:
            detail = get_email_detail_imap(
                account['email'],
                account['client_id'],
                account['refresh_token'],
                message_id,
                folder,
                proxy_url,
                fallback_proxy_urls,
            )
            if detail:
                pieces.extend([detail.get('subject', ''), detail.get('from', ''), detail.get('to', ''), detail.get('body', '')])
            else:
                return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), 'IMAP 邮件详情获取失败'
    except Exception as exc:
        return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), sanitize_error_details(str(exc))

    return clean_disabled_check_text(' '.join(str(piece or '') for piece in pieces)), ''


def summarize_disabled_check_email_item(email_item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': email_item.get('id', ''),
        'folder': email_item.get('folder', ''),
        'subject': email_item.get('subject', '') or '无主题',
        'from': email_item.get('from', '') or '',
        'date': email_item.get('date', '') or '',
    }


def enrich_disabled_check_match_summary(summary: Dict[str, Any], match_codes: List[str]) -> Dict[str, Any]:
    enriched = dict(summary)
    enriched['match_codes'] = match_codes
    enriched['match_labels'] = [
        DISABLED_ACCOUNT_CHECK_RULE_LABELS.get(code, code)
        for code in match_codes
    ]
    return enriched


def scan_account_for_disabled_notice_with_proxy(account: Dict[str, Any],
                                                recent_count: int,
                                                proxy_url: str = '',
                                                fallback_proxy_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    top = max(1, min(10, int(recent_count or DISABLED_ACCOUNT_CHECK_RECENT_COUNT)))
    result = fetch_account_emails_with_proxy(account, 'all', 0, top, proxy_url, fallback_proxy_urls or [])
    if not result.get('success'):
        return {
            'success': False,
            'status': 'error',
            'disabled': False,
            'error': stringify_disabled_check_error(result.get('error') or result.get('details')),
            'checked_count': 0,
            'checked_emails': [],
            'matched_emails': [],
        }

    emails = list(result.get('emails') or [])[:top]
    checked_emails = []
    matched_emails = []
    detail_errors = []
    for email_item in emails:
        summary = summarize_disabled_check_email_item(email_item)
        checked_emails.append(summary)
        text, detail_error = build_disabled_check_detail_text(account, email_item, proxy_url, fallback_proxy_urls)
        if detail_error:
            detail_errors.append({
                'subject': summary.get('subject', ''),
                'error': detail_error,
            })
        match_codes = get_disabled_account_notice_match_codes(text)
        if disabled_match_codes_indicate_disabled(match_codes):
            matched_emails.append(enrich_disabled_check_match_summary(summary, match_codes))

    disabled = bool(matched_emails)
    payload = {
        'success': True,
        'status': 'disabled' if disabled else 'active',
        'disabled': disabled,
        'checked_count': len(checked_emails),
        'checked_emails': checked_emails,
        'matched_emails': matched_emails,
    }
    if detail_errors:
        payload['detail_errors'] = detail_errors
    return payload


def scan_account_for_disabled_notice(account: Dict[str, Any],
                                     recent_count: int = DISABLED_ACCOUNT_CHECK_RECENT_COUNT) -> Dict[str, Any]:
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    return scan_account_for_disabled_notice_with_proxy(account, recent_count, proxy_url, fallback_proxy_urls)


def build_disabled_check_row_result(item: Dict[str, Any], recent_count: int) -> Dict[str, Any]:
    email_addr = item.get('email', '')
    normalized_email = item.get('normalized_email', '')
    base_result = {
        'line': item.get('line'),
        'email': email_addr,
        'normalized_email': normalized_email,
        'found': False,
        'account_id': None,
        'account_status': '',
        'disabled': False,
        'status': 'invalid',
        'status_label': '格式无效',
        'checked_count': 0,
        'checked_emails': [],
        'matched_emails': [],
        'error': '',
    }
    if not normalized_email or '@' not in normalized_email:
        return base_result

    account = get_account_by_email(normalized_email)
    if not account:
        return {
            **base_result,
            'status': 'not_found',
            'status_label': '未导入系统',
            'error': '该邮箱不在当前系统账号库中',
        }

    scan_result = scan_account_for_disabled_notice(account, recent_count)
    disabled = bool(scan_result.get('disabled'))
    if scan_result.get('success'):
        status = 'disabled' if disabled else 'active'
        status_label = '已停用' if disabled else '未命中停用通知'
        error = ''
    else:
        status = 'error'
        status_label = '检测失败'
        error = scan_result.get('error') or '检测失败'

    return {
        **base_result,
        'found': True,
        'account_id': account.get('id'),
        'account_status': normalize_account_status(account.get('status', 'active')),
        'disabled': disabled,
        'status': status,
        'status_label': status_label,
        'checked_count': int(scan_result.get('checked_count') or 0),
        'checked_emails': scan_result.get('checked_emails') or [],
        'matched_emails': scan_result.get('matched_emails') or [],
        'detail_errors': scan_result.get('detail_errors') or [],
        'error': error,
    }


def build_group_disabled_check_row_result(account: Dict[str, Any], index: int, group_id: int,
                                          group_name: str, recent_count: int, proxy_url: str = '',
                                          fallback_proxy_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    email_addr = account.get('email', '')
    normalized_email = normalize_email_address(email_addr)
    base_result = {
        'line': index,
        'email': email_addr,
        'normalized_email': normalized_email,
        'found': False,
        'account_id': account.get('id'),
        'account_status': normalize_account_status(account.get('status', 'active')),
        'disabled': False,
        'status': 'invalid',
        'status_label': '格式无效',
        'checked_count': 0,
        'checked_emails': [],
        'matched_emails': [],
        'detail_errors': [],
        'error': '',
        'group_id': group_id,
        'group_name': group_name,
        'marked_inactive': False,
        'account_status_after': normalize_account_status(account.get('status', 'active')),
    }
    if not normalized_email or '@' not in normalized_email:
        return base_result

    try:
        scan_result = scan_account_for_disabled_notice_with_proxy(
            dict(account),
            recent_count,
            proxy_url,
            fallback_proxy_urls or [],
        )
    except Exception as exc:
        return {
            **base_result,
            'found': True,
            'status': 'error',
            'status_label': '检测失败',
            'error': sanitize_error_details(str(exc)) or '检测失败',
        }

    disabled = bool(scan_result.get('disabled'))
    if scan_result.get('success'):
        status = 'disabled' if disabled else 'active'
        status_label = '已停用' if disabled else '未命中停用通知'
        error = ''
    else:
        status = 'error'
        status_label = '检测失败'
        error = scan_result.get('error') or '检测失败'

    return {
        **base_result,
        'found': True,
        'disabled': disabled,
        'status': status,
        'status_label': status_label,
        'checked_count': int(scan_result.get('checked_count') or 0),
        'checked_emails': scan_result.get('checked_emails') or [],
        'matched_emails': scan_result.get('matched_emails') or [],
        'detail_errors': scan_result.get('detail_errors') or [],
        'error': error,
    }


def build_disabled_check_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid_results = [row for row in results if row.get('status') != 'invalid']
    found_results = [row for row in valid_results if row.get('found')]
    checked_results = [row for row in found_results if row.get('status') in {'active', 'disabled'}]
    disabled_count = sum(1 for row in checked_results if row.get('disabled'))
    checked_count = len(checked_results)
    disabled_rate = round((disabled_count / checked_count) * 100, 2) if checked_count else 0.0
    return {
        'input_count': len(results),
        'valid_count': len(valid_results),
        'found_count': len(found_results),
        'checked_count': checked_count,
        'disabled_count': disabled_count,
        'active_count': max(0, checked_count - disabled_count),
        'not_found_count': sum(1 for row in valid_results if row.get('status') == 'not_found'),
        'invalid_count': sum(1 for row in results if row.get('status') == 'invalid'),
        'error_count': sum(1 for row in results if row.get('status') == 'error'),
        'disabled_rate': disabled_rate,
    }


def build_disabled_check_api_documentation() -> Dict[str, Any]:
    return {
        'endpoint': '/api/external/accounts/disabled-check',
        'method': 'POST',
        'auth': {
            'type': 'API Key',
            'header': 'X-API-Key: <your-api-key>',
            'query_aliases': ['api_key', 'apikey', 'api_token'],
        },
        'request_body': {
            'email': 'string，可选；单个邮箱账号，和 emails/text 可合并使用',
            'emails': 'string[] 或多行 string，可选；多个邮箱账号',
            'text': 'string，可选；多行邮箱账号，每行一个',
            'recent_count': f'int，可选；每个账号检测最近几封邮件，默认 {DISABLED_ACCOUNT_CHECK_RECENT_COUNT}，范围 1-10',
        },
        'response_fields': {
            'success': '接口是否成功处理请求',
            'recent_count': '本次每个账号检测的最近邮件数量',
            'summary.input_count': '输入行数',
            'summary.valid_count': '格式有效的输入数',
            'summary.found_count': '在本系统账号库中找到的账号数',
            'summary.checked_count': '实际完成邮件检测的账号数',
            'summary.disabled_count': '检测为停用/封禁的账号数',
            'summary.disabled_rate': '停用账号占已检测账号的百分比',
            'results[].status': 'invalid/not_found/active/disabled/error',
            'results[].disabled': '是否命中停用通知',
            'results[].matched_emails': '命中的最近邮件摘要，包含 match_codes 和 match_labels',
        },
        'detection_rules': DISABLED_ACCOUNT_CHECK_MATCH_RULES,
        'notes': [
            '接口只检测已导入本系统且有可用凭据的邮箱账号；未导入账号返回 not_found。',
            f'检测会读取 inbox 和 junkemail 最近邮件并按时间合并，默认检查最近 {DISABLED_ACCOUNT_CHECK_RECENT_COUNT} 封。',
            '发件人 trustandsafety@tm.openai.com 会作为命中依据返回；真正判定 disabled=true 需要同时命中标题/正文/条款封禁类停用信号之一。',
            '该对外接口只返回检测结果，不会自动修改账号 status；需要修改状态时请调用内部页面批量操作或另行接入状态更新接口。',
        ],
    }


def build_disabled_check_payload(data: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[tuple[Dict[str, Any], int]]]:
    raw_emails = data.get('emails')
    if data.get('email') is not None:
        if isinstance(raw_emails, list):
            raw_emails = [data.get('email')] + raw_emails
        elif raw_emails:
            raw_emails = f"{data.get('email')}\n{raw_emails}"
        else:
            raw_emails = [data.get('email')]

    input_items = normalize_disabled_check_email_inputs(raw_emails, data.get('text', ''))
    if not input_items:
        return None, ({'success': False, 'error': '请输入要检测的邮箱账号'}, 400)

    if len(input_items) > DISABLED_ACCOUNT_CHECK_MAX_EMAILS:
        return None, ({
            'success': False,
            'error': f'单次最多检测 {DISABLED_ACCOUNT_CHECK_MAX_EMAILS} 个邮箱账号',
            'max_count': DISABLED_ACCOUNT_CHECK_MAX_EMAILS,
        }, 400)

    recent_count = normalize_disabled_check_recent_count(data.get('recent_count'))

    cached_results: Dict[str, Dict[str, Any]] = {}
    results = []
    for item in input_items:
        normalized_email = item.get('normalized_email', '')
        if normalized_email and normalized_email in cached_results:
            cached = dict(cached_results[normalized_email])
            cached['line'] = item.get('line')
            cached['email'] = item.get('email')
            results.append(cached)
            continue

        row_result = build_disabled_check_row_result(item, recent_count)
        if normalized_email:
            cached_results[normalized_email] = dict(row_result)
        results.append(row_result)

    return {
        'success': True,
        'recent_count': recent_count,
        'results': results,
        'summary': build_disabled_check_summary(results),
    }, None


def build_group_disabled_check_payload(group_id: int, data: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[tuple[Dict[str, Any], int]]]:
    group = get_group_by_id(group_id)
    if not group:
        return None, ({'success': False, 'error': '分组不存在'}, 404)

    recent_count = normalize_disabled_check_recent_count(data.get('recent_count'))

    accounts = load_accounts(group_id)
    group_name = group.get('name', '')
    proxy_url = get_group_proxy_url(group)
    fallback_proxy_urls = get_group_proxy_failover_urls(group)
    results: List[Dict[str, Any]] = []
    disabled_account_ids: List[int] = []
    if accounts:
        ordered_results: List[Optional[Dict[str, Any]]] = [None] * len(accounts)
        worker_count = min(len(accounts), DISABLED_GROUP_CHECK_MAX_WORKERS)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix='disabled-check-account') as executor:
            future_to_index = {
                executor.submit(
                    build_group_disabled_check_row_result,
                    dict(account),
                    index,
                    group_id,
                    group_name,
                    recent_count,
                    proxy_url,
                    fallback_proxy_urls,
                ): index - 1
                for index, account in enumerate(accounts, start=1)
            }
            for future in as_completed(future_to_index):
                result_index = future_to_index[future]
                ordered_results[result_index] = future.result()
        results = [row for row in ordered_results if row is not None]

    for row_result in results:
        if row_result.get('found') and row_result.get('disabled') and row_result.get('account_id'):
            disabled_account_ids.append(int(row_result['account_id']))

    update_result = {
        'success': True,
        'status': 'inactive',
        'updated_count': 0,
        'updated_accounts': [],
        'unchanged_count': 0,
        'missing_ids': [],
    }
    if disabled_account_ids:
        update_result = update_accounts_status_by_ids(disabled_account_ids, 'inactive')
        if not update_result.get('success'):
            return None, ({
                'success': False,
                'error': update_result.get('error') or '标注停用账号失败',
                'results': results,
                'summary': build_disabled_check_summary(results),
            }, 500)

    updated_ids = {
        int(account.get('id'))
        for account in update_result.get('updated_accounts', [])
        if account.get('id') is not None
    }
    disabled_id_set = set(disabled_account_ids)
    for row_result in results:
        account_id = row_result.get('account_id')
        if not account_id:
            continue
        if int(account_id) in disabled_id_set:
            row_result['account_status_after'] = 'inactive'
        if int(account_id) in updated_ids:
            row_result['marked_inactive'] = True

    summary = build_disabled_check_summary(results)
    summary['marked_inactive_count'] = int(update_result.get('updated_count') or 0)
    summary['already_inactive_count'] = int(update_result.get('unchanged_count') or 0)
    summary['group_account_count'] = len(accounts)

    return {
        'success': True,
        'group': {
            'id': group.get('id'),
            'name': group.get('name', ''),
        },
        'recent_count': recent_count,
        'results': results,
        'summary': summary,
        'status_update': {
            'status': update_result.get('status'),
            'updated_count': update_result.get('updated_count', 0),
            'updated_accounts': update_result.get('updated_accounts', []),
            'unchanged_count': update_result.get('unchanged_count', 0),
            'missing_ids': update_result.get('missing_ids', []),
        },
        'message': f"当前分组检测完成：发现停用 {summary['disabled_count']} 个，本次标注 {summary['marked_inactive_count']} 个",
    }, None


@app.route('/api/accounts/disabled-check', methods=['POST'])
@login_required
def api_batch_check_disabled_accounts():
    """批量检测账号最近邮件中是否包含停用通知。"""
    data = request.get_json(silent=True) or {}
    payload, error = build_disabled_check_payload(data)
    if error:
        return jsonify(error[0])
    return jsonify(payload)


@app.route('/api/groups/<int:group_id>/accounts/disabled-check', methods=['POST'])
@login_required
def api_check_group_disabled_accounts(group_id: int):
    """检测指定分组内所有账号，命中停用通知后自动标注为停用。"""
    data = request.get_json(silent=True) or {}
    if data.get('async') is False:
        payload, error = build_group_disabled_check_payload(group_id, data)
        if error:
            return jsonify(error[0]), error[1]
        return jsonify(payload)

    group = get_group_by_id(group_id)
    if not group:
        return jsonify({'success': False, 'error': '分组不存在'}), 404

    cleanup_disabled_group_check_tasks()
    task_id = uuid.uuid4().hex
    now_iso = datetime.now(timezone.utc).isoformat()
    recent_count = normalize_disabled_check_recent_count(data.get('recent_count'))
    create_disabled_group_check_task_record(task_id, group_id, group.get('name', ''), recent_count, now_iso)
    with DISABLED_GROUP_CHECK_TASK_LOCK:
        DISABLED_GROUP_CHECK_TASKS[task_id] = {
            'success': True,
            'task_id': task_id,
            'group_id': group_id,
            'group_name': group.get('name', ''),
            'status': 'running',
            'recent_count': recent_count,
            'created_ts': time.time(),
            'created_at': now_iso,
            'updated_at': now_iso,
            'payload': None,
            'error': '',
        }

    app.logger.info('group disabled-check task accepted: task_id=%s group_id=%s', task_id, group_id)
    log_disabled_group_check_event(f'task accepted: task_id={task_id} group_id={group_id}')
    worker = threading.Thread(
        target=run_group_disabled_check_task,
        args=(task_id, group_id, dict(data)),
        name=f'disabled-check-group-{group_id}-{task_id[:8]}',
        daemon=True,
    )
    worker.start()
    return jsonify({
        'success': True,
        'status': 'running',
        'task_id': task_id,
        'group_id': group_id,
        'message': '当前分组停用检测任务已启动',
    }), 202


@app.route('/api/groups/disabled-check-tasks/<task_id>', methods=['GET'])
@login_required
def api_get_group_disabled_check_task(task_id: str):
    task = get_disabled_group_check_task(task_id)
    if not task:
        persisted_task = get_persisted_disabled_group_check_task(task_id, include_payload=True)
        if not persisted_task:
            return jsonify({'success': False, 'error': '检测任务不存在或已过期'}), 404
        payload = persisted_task.get('payload')
        if persisted_task.get('status') == 'completed' and isinstance(payload, dict):
            return jsonify({
                **payload,
                'task_id': persisted_task.get('task_id'),
                'task_status': 'completed',
                'history_record': persisted_task,
            })
        if persisted_task.get('status') == 'failed':
            return jsonify({
                'success': False,
                'task_id': persisted_task.get('task_id'),
                'task_status': 'failed',
                'error': persisted_task.get('error') or '当前分组停用检测失败',
                'payload': payload,
                'history_record': persisted_task,
            }), 500
        return jsonify({
            'success': True,
            'task_id': persisted_task.get('task_id'),
            'group_id': persisted_task.get('group_id'),
            'group_name': persisted_task.get('group_name', ''),
            'task_status': 'running',
            'status': 'running',
            'message': '当前分组停用检测仍在执行',
            'created_at': persisted_task.get('created_at'),
            'updated_at': persisted_task.get('updated_at'),
        })

    payload = task.get('payload')
    if task.get('status') == 'completed' and isinstance(payload, dict):
        return jsonify({
            **payload,
            'task_id': task.get('task_id'),
            'task_status': 'completed',
        })

    if task.get('status') == 'failed':
        return jsonify({
            'success': False,
            'task_id': task.get('task_id'),
            'task_status': 'failed',
            'error': task.get('error') or '当前分组停用检测失败',
            'payload': payload,
        }), int(task.get('http_status') or 500)

    return jsonify({
        'success': True,
        'task_id': task.get('task_id'),
        'group_id': task.get('group_id'),
        'task_status': 'running',
        'status': 'running',
        'message': '当前分组停用检测仍在执行',
        'created_at': task.get('created_at'),
        'updated_at': task.get('updated_at'),
    })


@app.route('/api/groups/disabled-check-tasks/history', methods=['GET'])
@login_required
def api_list_group_disabled_check_tasks():
    try:
        group_id = int(request.args.get('group_id') or 0)
    except (TypeError, ValueError):
        group_id = 0
    try:
        limit = int(request.args.get('limit') or 50)
    except (TypeError, ValueError):
        limit = 50

    tasks = list_disabled_group_check_task_records(group_id=group_id or None, limit=limit)
    return jsonify({
        'success': True,
        'tasks': tasks,
        'total': len(tasks),
    })


@app.route('/api/groups/<int:group_id>/accounts/disabled-check-sync', methods=['POST'])
@login_required
def api_check_group_disabled_accounts_sync(group_id: int):
    """兼容同步检测入口，主要用于测试和内部诊断。"""
    data = request.get_json(silent=True) or {}
    payload, error = build_group_disabled_check_payload(group_id, data)
    if error:
        return jsonify(error[0]), error[1]
    return jsonify(payload)


@app.route('/api/external/accounts/disabled-check', methods=['POST'])
@csrf_exempt
@api_key_required
def api_external_check_disabled_accounts():
    """
    对外 API：检测账号最近邮件中是否包含停用通知。

    认证：
    - Header: X-API-Key: <your-api-key>
    - 或 Query: api_key / apikey / api_token

    JSON 请求体：
    - email: 单个邮箱账号，可选
    - emails: 多个邮箱账号，可传数组或多行字符串，可选
    - text: 多行邮箱账号，每行一个，可选
    - recent_count: 每个账号检测最近几封邮件，默认 10，范围 1-10

    检测依据：
    - 发件人包含 trustandsafety@tm.openai.com
    - 标题包含 OpenAI - Access Deactivated [C-...]
    - 正文包含 Your account has been banned because recent activity violated our Terms and Usage Policies.

    说明：
    - 只检测已导入系统的账号，不会自动修改账号 status。
    - 响应中的 documentation 字段包含完整对接说明和字段解释。
    """
    data = request.get_json(silent=True) or {}
    payload, error = build_disabled_check_payload(data)
    if error:
        error_payload, status_code = error
        error_payload['documentation'] = build_disabled_check_api_documentation()
        return jsonify(error_payload), status_code

    payload['documentation'] = build_disabled_check_api_documentation()
    return jsonify(payload)


@app.route('/api/emails/<email_addr>')
@login_required
def api_get_emails(email_addr):
    """获取邮件列表（支持分页，不使用缓存）"""
    account = get_account_by_email(email_addr)

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "账号不存在",
            "NotFoundError",
            404,
            f"email={email_addr}"
        )
        return jsonify({'success': False, 'error': error_payload})

    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    local_retention_request = is_local_retention_request()
    if local_retention_request:
        skip = parse_non_negative_int(request.args.get('skip', 0), 0)
        top = parse_non_negative_int(request.args.get('top', 20), 20)
        return jsonify(fetch_retained_normal_mail_list(account, folder, skip, top))
    skip = parse_non_negative_int(request.args.get('skip', 0), 0)
    top = parse_non_negative_int(request.args.get('top', 20), 20, 50)
    result = fetch_account_emails(account, folder, skip, top)
    return jsonify(result)


@app.route('/api/emails/mark-read', methods=['POST'])
@login_required
def api_mark_emails_read():
    """批量标记邮件为已读"""
    data = request.json or {}
    email_addr = str(data.get('email') or '').strip()
    method = str(data.get('method') or 'graph').strip().lower()
    fallback_folder = normalize_folder_name(data.get('folder', 'inbox'))
    raw_items = data.get('items') if data.get('items') is not None else data.get('ids', [])
    items = normalize_email_action_items(raw_items, fallback_folder)
    if not email_addr or not items:
        return jsonify({'success': False, 'error': '参数不完整'})

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    if account.get('account_type') == 'imap':
        return jsonify(mark_imap_account_emails_read(account, items, proxy_url))

    graph_items, imap_items = split_email_action_items_by_method(items, method)
    results = []
    if graph_items:
        results.append(mark_graph_items_read(account, graph_items, proxy_url, fallback_proxy_urls))
    if imap_items:
        results.append(mark_oauth_imap_items_read(account, imap_items, proxy_url, fallback_proxy_urls))
    return jsonify(merge_email_action_results(results))


@app.route('/api/emails/retain-bodies', methods=['POST'])
@login_required
def api_retain_email_bodies():
    """按服务器端上限为已保留的普通邮箱列表行补齐正文。"""
    data = request.json or {}
    email_addr = str(data.get('email') or '').strip()
    fallback_folder = normalize_folder_name(data.get('folder', 'inbox'))
    fallback_method = str(data.get('method') or 'graph').strip().lower()
    raw_items = data.get('items')
    if raw_items is None:
        raw_items = data.get('ids', [])
    items = normalize_body_retention_items(raw_items, fallback_folder, fallback_method)
    if not email_addr or not items:
        return jsonify({'success': False, 'error': '参数不完整'})

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})
    if not is_normal_mail_local_retention_enabled():
        return jsonify({
            'success': False,
            'error': '普通邮箱本地保留未启用',
            'local_retention_enabled': False,
            'cached_count': 0,
            'skipped_count': len(items),
            'failed_count': 0,
            'limit': RETAINED_MAIL_BODY_FETCH_LIMIT,
            'results': [],
            'errors': [],
        })

    return jsonify(retain_normal_mail_bodies(account, items))


@app.route('/api/emails/delete', methods=['POST'])
@login_required
def api_delete_emails():
    """批量删除邮件（永久删除）"""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    email_addr = data.get('email', '')
    message_ids = data.get('ids', [])
    
    if not email_addr or not message_ids:
        return jsonify({'success': False, 'error': '参数不完整'})

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)

    # 1. 优先尝试 Graph API
    if account.get('account_type') == 'imap':
        return jsonify({'success': False, 'error': 'IMAP 账号暂不支持批量删除邮件'})

    graph_res = delete_emails_graph(account['client_id'], account['refresh_token'], message_ids, proxy_url, fallback_proxy_urls)
    if graph_res['success']:
        delete_retained_normal_mail_rows(account, message_ids, graph_res, fallback_id_mode='graph')
        return jsonify(graph_res)

    # 如果是代理错误，不再回退 IMAP
    graph_error = graph_res.get('error', '')
    if isinstance(graph_error, str) and 'ProxyError' in graph_error:
        return jsonify(graph_res)
    
    # 2. 尝试 IMAP 回退（新服务器）
    imap_res = delete_emails_imap(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        message_ids,
        IMAP_SERVER_NEW,
        proxy_url,
        fallback_proxy_urls,
    )
    if imap_res['success']:
        delete_retained_normal_mail_rows(account, message_ids, imap_res, fallback_id_mode='uid')
        return jsonify(imap_res)

    # 3. 尝试 IMAP 回退（旧服务器）
    imap_old_res = delete_emails_imap(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        message_ids,
        IMAP_SERVER_OLD,
        proxy_url,
        fallback_proxy_urls,
    )
    if imap_old_res['success']:
        delete_retained_normal_mail_rows(account, message_ids, imap_old_res, fallback_id_mode='uid')
        return jsonify(imap_old_res)

    # 所有方式均失败，返回 Graph API 的错误
    return jsonify(graph_res)



@app.route('/api/email/<email_addr>/<path:message_id>/raw')
@login_required
def api_get_raw_email(email_addr, message_id):
    """获取原始 MIME 邮件源码。"""
    account = get_account_by_email(email_addr)

    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    method = request.args.get('method', 'graph')
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)

    raw_content = None
    if account.get('account_type') == 'imap':
        raw_content = get_raw_email_imap_generic(
            account['email'],
            account.get('imap_password', ''),
            account.get('imap_host', ''),
            account.get('imap_port', 993),
            message_id,
            folder,
            account.get('provider', 'custom'),
            proxy_url
        )
    elif method == 'graph':
        raw_content = get_raw_email_graph(
            account['client_id'],
            account['refresh_token'],
            message_id,
            proxy_url,
            fallback_proxy_urls,
        )
        if raw_content is None:
            raw_content = get_raw_email_imap(
                account['email'],
                account['client_id'],
                account['refresh_token'],
                message_id,
                folder,
                proxy_url,
                fallback_proxy_urls,
            )
    else:
        raw_content = get_raw_email_imap(
            account['email'],
            account['client_id'],
            account['refresh_token'],
            message_id,
            folder,
            proxy_url,
            fallback_proxy_urls,
        )

    if raw_content is None:
        return jsonify({'success': False, 'error': '获取原始邮件失败'})

    if isinstance(raw_content, str):
        raw_text = raw_content
    else:
        raw_text = bytes(raw_content).decode('utf-8', errors='replace')

    safe_message_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(message_id)).strip('_') or 'message'
    return jsonify({
        'success': True,
        'raw': raw_text,
        'filename': f'{safe_message_id}.eml',
        'warning': '原始邮件包含完整邮件头和路由信息，请谨慎分享。'
    })


@app.route('/api/email/<email_addr>/<path:message_id>')
@login_required
def api_get_email_detail(email_addr, message_id):
    """获取邮件详情"""
    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    method = request.args.get('method', 'graph')
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    id_mode = str(request.args.get('id_mode') or '').strip().lower()
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)

    if is_prefer_local_detail_request() and is_normal_mail_local_retention_enabled():
        retained_detail = fetch_retained_normal_mail_detail(account, folder, message_id, id_mode)
        if retained_detail:
            return jsonify(retained_detail)

    if account.get('account_type') == 'imap':
        result = fetch_imap_account_detail_response(
            account, folder, message_id, method, id_mode, proxy_url
        )
        return jsonify(result)

    if method == 'graph':
        result = fetch_graph_detail_response(
            account, folder, message_id, method, id_mode, proxy_url, fallback_proxy_urls
        )
        if result:
            return jsonify(result)

    result = fetch_oauth_imap_detail_response(
        account, folder, message_id, method, id_mode, proxy_url, fallback_proxy_urls
    )
    if result:
        return jsonify(result)
    return jsonify({'success': False, 'error': '获取邮件详情失败'})


def download_email_attachment_for_account(account, method, message_id, attachment_id, folder, proxy_url, fallback_proxy_urls, id_mode=''):
    if account.get('account_type') == 'imap':
        return download_email_attachment_imap_generic_result(
            account['email'],
            account.get('imap_password', ''),
            account.get('imap_host', ''),
            account.get('imap_port', 993),
            message_id,
            attachment_id,
            folder,
            account.get('provider', 'custom'),
            proxy_url
        )

    if method == 'graph':
        return download_email_attachment_graph_result(
            account['client_id'],
            account['refresh_token'],
            message_id,
            attachment_id,
            proxy_url,
            fallback_proxy_urls,
        )

    return download_email_attachment_imap_result(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        message_id,
        attachment_id,
        folder,
        proxy_url,
        fallback_proxy_urls,
        id_mode,
    )


def get_email_attachment_metadata_for_download(account, method, message_id, folder, proxy_url, fallback_proxy_urls, id_mode=''):
    if account.get('account_type') == 'imap':
        detail_result = get_email_detail_imap_generic_result(
            account['email'],
            account.get('imap_password', ''),
            account.get('imap_host', ''),
            account.get('imap_port', 993),
            message_id,
            folder,
            account.get('provider', 'custom'),
            proxy_url
        )
        if detail_result.get('success'):
            return {'success': True, 'attachments': detail_result.get('email', {}).get('attachments', [])}
        return {'success': False, 'error': detail_result.get('error', '获取附件列表失败')}

    if method == 'graph':
        attachments = get_email_attachments_graph(
            account['client_id'],
            account['refresh_token'],
            message_id,
            proxy_url,
            fallback_proxy_urls,
        )
        if attachments is None:
            return {'success': False, 'error': '获取附件列表失败'}
        return {'success': True, 'attachments': attachments}

    detail = get_email_detail_imap(
        account['email'],
        account['client_id'],
        account['refresh_token'],
        message_id,
        folder,
        proxy_url,
        fallback_proxy_urls,
        id_mode or 'uid',
    )
    if detail:
        return {'success': True, 'attachments': detail.get('attachments', [])}
    return {'success': False, 'error': '获取附件列表失败'}


def build_zip_attachment_name(filename, used_names):
    filename = sanitize_attachment_filename(filename, 'attachment')
    name_root, dot, extension = filename.rpartition('.')
    if not dot or not name_root:
        name_root = filename
        extension = ''

    candidate = filename
    counter = 2
    while candidate in used_names:
        suffix = f" ({counter})"
        candidate = f"{name_root}{suffix}.{extension}" if extension else f"{name_root}{suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


ZIP_STREAM_CHUNK_SIZE = 64 * 1024


class StreamingZipBuffer:
    def __init__(self):
        self._position = 0
        self._chunks = []

    def write(self, data):
        if not data:
            return 0
        chunk = bytes(data)
        self._chunks.append(chunk)
        self._position += len(chunk)
        return len(chunk)

    def tell(self):
        return self._position

    def seek(self, *_args, **_kwargs):
        raise OSError('streaming zip buffer does not support seek')

    def flush(self):
        pass

    def drain(self):
        chunks = self._chunks
        self._chunks = []
        return chunks


def iter_zip_content_chunks(content, chunk_size=ZIP_STREAM_CHUNK_SIZE):
    if isinstance(content, str):
        content = content.encode('utf-8')
    content = content or b''
    for offset in range(0, len(content), chunk_size):
        yield content[offset:offset + chunk_size]


def drain_streaming_zip_buffer(zip_buffer):
    for chunk in zip_buffer.drain():
        if chunk:
            yield chunk


def stringify_attachment_download_error(error):
    if isinstance(error, dict):
        return str(error.get('message') or error.get('code') or error)
    return str(error or '获取附件失败')


def stream_email_attachments_zip(account, method, message_id, attachments, folder, proxy_url, fallback_proxy_urls, id_mode=''):
    import zipfile

    zip_buffer = StreamingZipBuffer()
    used_names = set()
    error_lines = []

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for attachment in attachments:
            result = download_email_attachment_for_account(
                account,
                method,
                message_id,
                attachment.get('id'),
                folder,
                proxy_url,
                fallback_proxy_urls,
                attachment.get('id_mode') or id_mode,
            )
            if not result.get('success'):
                attachment_name = sanitize_attachment_filename(
                    attachment.get('name', ''),
                    str(attachment.get('id') or 'attachment')
                )
                error_lines.append(f"{attachment_name}: {stringify_attachment_download_error(result.get('error'))}")
                continue

            zip_name = build_zip_attachment_name(
                result.get('filename') or attachment.get('name') or 'attachment',
                used_names
            )
            content = result.get('content', b'') or b''
            with archive.open(zip_name, 'w') as entry:
                yield from drain_streaming_zip_buffer(zip_buffer)
                for chunk in iter_zip_content_chunks(content):
                    entry.write(chunk)
                    yield from drain_streaming_zip_buffer(zip_buffer)
            yield from drain_streaming_zip_buffer(zip_buffer)

        if error_lines:
            error_name = build_zip_attachment_name('download-errors.txt', used_names)
            error_content = '\n'.join(error_lines)
            with archive.open(error_name, 'w') as entry:
                yield from drain_streaming_zip_buffer(zip_buffer)
                for chunk in iter_zip_content_chunks(error_content):
                    entry.write(chunk)
                    yield from drain_streaming_zip_buffer(zip_buffer)
            yield from drain_streaming_zip_buffer(zip_buffer)

    yield from drain_streaming_zip_buffer(zip_buffer)


@app.route('/api/email/<email_addr>/<path:message_id>/attachments/download-all')
@login_required
def api_download_all_email_attachments(email_addr, message_id):
    """打包下载邮件所有附件"""
    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    method = request.args.get('method', 'graph')
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    id_mode = str(request.args.get('id_mode') or '').strip().lower()
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    metadata_result = get_email_attachment_metadata_for_download(
        account,
        method,
        message_id,
        folder,
        proxy_url,
        fallback_proxy_urls,
        id_mode,
    )
    if not metadata_result.get('success'):
        return jsonify({'success': False, 'error': metadata_result.get('error', '获取附件列表失败')})

    attachments = [attachment for attachment in metadata_result.get('attachments', []) if attachment.get('id')]
    if not attachments:
        return jsonify({'success': False, 'error': '没有可下载附件'})

    response = Response(
        stream_email_attachments_zip(
            account,
            method,
            message_id,
            attachments,
            folder,
            proxy_url,
            fallback_proxy_urls,
            id_mode,
        ),
        mimetype='application/zip'
    )
    response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''attachments.zip"
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/email/<email_addr>/<path:message_id>/attachments/<path:attachment_id>')
@login_required
def api_download_email_attachment(email_addr, message_id, attachment_id):
    """下载邮件附件"""
    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    method = request.args.get('method', 'graph')
    folder = normalize_folder_name(request.args.get('folder', 'inbox'))
    id_mode = str(request.args.get('id_mode') or '').strip().lower()
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    result = download_email_attachment_for_account(
        account,
        method,
        message_id,
        attachment_id,
        folder,
        proxy_url,
        fallback_proxy_urls,
        id_mode,
    )

    if not result.get('success'):
        return jsonify({'success': False, 'error': result.get('error', '获取附件失败')})

    filename = sanitize_attachment_filename(result.get('filename', ''), 'attachment')
    encoded_filename = quote(filename)
    content_type = result.get('content_type', 'application/octet-stream') or 'application/octet-stream'
    response = Response(result.get('content', b''), mimetype=content_type)
    response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
    return response
