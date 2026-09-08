"""UI 改善の操作契約。API・保存値を変えず、キーボードと狭幅でも同じ操作へ到達する。"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from mock_api import CONVERSATIONS_LIST, install_api_mocks


def _contrast(foreground, background):
    def luminance(value):
        rgb = [float(c) / 255 for c in re.findall(r'[\d.]+', value)[:3]]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * weight for c, weight in zip(linear, [.2126, .7152, .0722]))
    a, b = sorted([luminance(foreground), luminance(background)])
    return (b + .05) / (a + .05)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_action_surfaces_keep_readable_text_in_both_themes(page, web_base_url, theme):
    install_api_mocks(page)
    page.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}')")
    page.goto(f"{web_base_url}/chat.html")
    page.locator('#input').fill('根拠を確認したい')
    page.locator('#send').click()
    expect(page.locator('#messages .sources')).to_be_visible()
    bubble = page.locator('.bubble-user').last
    colors = bubble.evaluate('(el) => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]')
    assert _contrast(*colors) >= 4.5
    page.goto(f"{web_base_url}/admin-settings.html")
    button = page.locator('#save')
    colors = button.evaluate('(el) => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]')
    assert _contrast(*colors) >= 4.5


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("saved,size", [(None, "16px"), ("小", "14px"), ("標準", "16px"),
                                       ("大", "18px"), ("特大", "20px"), ("未知値", "16px")])
def test_chat_font_choice_survives_reload_and_theme(page, web_base_url, theme, saved, size):
    install_api_mocks(page)
    page.add_init_script(f"localStorage.setItem('sherpa-theme', {json.dumps(theme)});")
    if saved is not None:
        page.add_init_script(f"localStorage.setItem('sherpa-chatfont', {json.dumps(saved)});")
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#messages")).to_have_css("font-size", size)
    page.locator("#themebtn").click()
    expect(page.locator("#messages")).to_have_css("font-size", size)
    page.reload()
    expect(page.locator("#messages")).to_have_css("font-size", size)
    assert page.evaluate("localStorage.getItem('sherpa-chatfont')") == saved


def test_chat_standard_font_before_javascript_matches_after(page, web_base_url):
    install_api_mocks(page)
    page.route("**/chat.js", lambda route: route.fulfill(content_type="text/javascript", body=""))
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#messages")).to_have_css("font-size", "16px")
    page.unroute("**/chat.js")
    page.reload()
    expect(page.locator("#messages")).to_have_css("font-size", "16px")


@pytest.mark.parametrize("pinned", [False, True])
def test_history_menu_reserves_title_and_keeps_open_action_separate(page, web_base_url, pinned):
    install_api_mocks(page)
    conversations = [dict(c) for c in CONVERSATIONS_LIST]
    conversations[0].update(title="長い会話タイトル・" * 12, pinned=pinned)
    page.route("**/conversations", lambda route: route.fulfill(json=conversations))
    page.add_init_script("localStorage.setItem('sherpa-cols', JSON.stringify({L:200,R:300}))")
    opened = []
    page.on("request", lambda req: opened.append(req.url) if urlparse(req.url).path == "/conversations/101" else None)
    page.goto(f"{web_base_url}/chat.html")
    row = page.locator('[data-open="101"]')
    trigger = row.locator('.conv-more')
    title = row.locator('.cmain')
    expect(trigger).to_be_visible()
    before = title.bounding_box()
    row.hover()
    assert title.bounding_box() == before
    trigger_box = trigger.bounding_box()
    assert trigger_box["width"] == 32 and trigger_box["height"] == 32
    assert before["x"] + before["width"] <= trigger_box["x"]
    trigger.focus()
    page.keyboard.press("Space")
    menu = page.locator('#conv-actions-101')
    expect(menu).to_be_visible()
    expect(menu.locator('[data-rename]')).to_be_focused()
    for selector in ('[data-pin]', '[data-sharecid]', '[data-del]'):
        page.keyboard.press("Tab")
        expect(menu.locator(selector)).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_be_focused()
    trigger.click()
    page.locator('#input').click()
    expect(menu).to_be_hidden()
    assert opened == []
    title.focus()
    page.keyboard.press("Enter")
    expect(page.locator('#conv-title')).to_have_text('消費税率の相談')
    assert len(opened) == 1


def test_history_rename_pin_delete_keep_requests_and_focus(page, web_base_url):
    install_api_mocks(page)
    conversations = [dict(c) for c in CONVERSATIONS_LIST]
    calls = []
    page.route("**/conversations", lambda route: route.fulfill(json=conversations))

    def mutate(route):
        req = route.request
        calls.append((req.method, urlparse(req.url).path, req.post_data_json))
        if req.method == "PATCH":
            conversations[0].update(req.post_data_json)
        elif req.method == "POST":
            conversations[0].update(req.post_data_json)
        elif req.method == "DELETE":
            conversations.pop(0)
        else:
            pytest.fail(f"補助操作で会話を開いています: {req.method} {req.url}")
        route.fulfill(json={"ok": True})

    page.route("**/conversations/101", mutate)
    page.route("**/conversations/101/pin", mutate)
    page.on('dialog', lambda d: d.accept('新しい名前') if d.type == 'prompt' else d.accept())
    page.goto(f"{web_base_url}/chat.html")
    trigger = page.locator('[data-conv-menu="101"]')
    trigger.click()
    page.locator('[data-rename="101"]').click()
    expect(page.locator('[data-open="101"] .t')).to_have_text('新しい名前')
    expect(trigger).to_be_focused()
    trigger.click()
    page.locator('[data-pin="101"]').click()
    expect(page.locator('[data-open="101"]')).to_have_class('conv pinned')
    expect(trigger).to_be_focused()
    trigger.click()
    page.locator('[data-del="101"]').click()
    expect(trigger).to_have_count(0)
    expect(page.locator('[data-conv-menu="202"]')).to_be_focused()
    assert calls == [('PATCH', '/conversations/101', {'title': '新しい名前'}),
                     ('POST', '/conversations/101/pin', {'pinned': True}),
                     ('DELETE', '/conversations/101', None)]


def test_history_share_dialog_keyboard_returns_to_trigger(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    trigger = page.locator('[data-conv-menu="101"]')
    trigger.click()
    page.locator('[data-sharecid="101"]').focus()
    page.keyboard.press("Enter")
    expect(page.locator('#share-overlay')).to_be_visible()
    expect(page.locator('#share-invitees')).to_be_focused()
    page.evaluate("async () => { const history = await import('./chat/history.js'); await history.loadConversations(); }")
    page.keyboard.press("Escape")
    expect(page.locator('#share-overlay')).to_be_hidden()
    expect(trigger).to_be_focused()
    # 受領共有の操作は従来通り pin/delete だけ。
    page.locator('[data-conv-menu="202"]').click()
    menu = page.locator('#conv-actions-202')
    expect(menu.locator('button')).to_have_count(2)
    expect(menu.locator('[data-rename], [data-sharecid]')).to_have_count(0)


def test_history_more_is_available_on_touch(browser, web_base_url):
    context = browser.new_context(viewport={"width": 1280, "height": 800}, has_touch=True)
    page = context.new_page()
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator('[data-conv-menu="101"]').tap()
    expect(page.locator('#conv-actions-101')).to_be_visible()
    page.locator('#input').tap()
    expect(page.locator('#conv-actions-101')).to_be_hidden()
    context.close()


def test_admin_vertical_tabs_manual_activation_and_hidden_items(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    tabs = page.locator('#admin-tabs')
    expect(tabs).to_have_attribute('aria-orientation', 'vertical')
    page.locator('[data-tab="provider"]').focus()
    page.keyboard.press('ArrowDown')
    expect(page.locator('[data-tab="models"]')).to_be_focused()
    expect(page.locator('[data-tab="provider"]')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#embed-frame-users')).not_to_have_attribute('src', re.compile('.+'))
    page.keyboard.press('End')
    expect(page.locator('[data-tab="status"]')).to_be_focused()
    expect(page.locator('#embed-frame-status')).not_to_have_attribute('src', re.compile('.+'))
    page.keyboard.press('Space')
    expect(page.locator('#tabpanel-status')).to_be_visible()
    expect(page.locator('#embed-frame-status')).to_have_attribute('src', 'status.html?embed=1')
    expect(page.locator('[data-tab="status"]')).to_have_attribute('tabindex', '0')
    expect(page.locator('[data-tab="provider"]')).to_have_attribute('tabindex', '-1')
    assert page.url.endswith('#status')
    page.locator('[data-tab="audit"]').evaluate('(el) => el.hidden = true')
    page.keyboard.press('ArrowUp')
    expect(page.locator('[data-tab="usage-page"]')).to_be_focused()
    page.keyboard.press('Home')
    page.keyboard.press('Enter')
    expect(page.locator('#tabpanel-provider')).to_be_visible()
    page.locator('[data-tab="usage"]').click()
    expect(page.locator('[data-tab="usage"]')).to_contain_text('利用統計の AI')
    assert page.url.endswith('#usage')
    expect(page.locator('#tabpanel-usage')).to_have_attribute('aria-labelledby', 'tab-btn-usage')


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('height', [300, 400, 450])
def test_admin_iframe_theme_and_low_viewport_do_not_overlap_save(page, web_base_url, theme, height):
    install_api_mocks(page)
    page.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}')")
    page.set_viewport_size({'width': 640, 'height': height})
    page.goto(f"{web_base_url}/admin-settings.html")
    page.locator('#depth-base-max-turns').fill('21')
    expect(page.locator('#unsaved-note')).to_be_visible()
    expect(page.locator('#admin-tabs')).to_have_css('position', 'static')
    nav, panel = page.locator('#admin-tabs').bounding_box(), page.locator('.admin-panels').bounding_box()
    assert nav['x'] + nav['width'] <= panel['x']
    page.locator('[data-tab="users"]').click()
    users = page.frame_locator('#embed-frame-users')
    expect(users.locator('html')).to_have_attribute('data-theme', theme)
    expect(users.locator('#table-wrap')).to_be_visible()
    for key in ['users', 'usage-page', 'audit', 'status']:
        page.locator(f'[data-tab="{key}"]').click()
        frame = page.locator(f'#embed-frame-{key}')
        box, save = frame.bounding_box(), page.locator('#save-bar').bounding_box()
        assert box['height'] > 100
        assert box['y'] + box['height'] <= save['y']
        expect(page.frame_locator(f'#embed-frame-{key}').locator('html')).to_have_attribute('data-theme', theme)
    page.locator('#themebtn').click()
    changed = 'dark' if theme == 'light' else 'light'
    expect(users.locator('html')).to_have_attribute('data-theme', changed)
    expect(page.frame_locator('#embed-frame-status').locator('html')).to_have_attribute('data-theme', changed)
    page.locator('#save').scroll_into_view_if_needed()
    expect(page.locator('#save')).to_be_in_viewport()


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('key,filename', [('users', 'admin-users'), ('usage-page', 'usage'),
                                        ('audit', 'audit'), ('status', 'status')])
def test_admin_embed_keeps_standalone_breadcrumb_and_reaches_end(page, web_base_url, theme, key, filename):
    install_api_mocks(page)
    page.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}')")
    page.set_viewport_size({'width': 640, 'height': 400})
    page.goto(f'{web_base_url}/{filename}.html')
    expect(page.locator('.crumb')).to_be_visible()
    page.goto(f'{web_base_url}/admin-settings.html')
    page.locator('[data-tab="' + key + '"]').click()
    frame = page.locator('#embed-frame-' + key)
    child = page.frame_locator('#embed-frame-' + key)
    expect(child.locator('h1')).to_be_visible()
    expect(child.locator('.crumb')).to_be_hidden()
    rows = {'users': '#user-tbody tr', 'usage-page': '#token-user-tbody tr',
            'audit': '#audit-tbody tr', 'status': '#health-tbody tr'}
    expect(child.locator(rows[key]).first).to_be_visible()
    # 実際の末尾の操作をキーボードでフォーカスし、親子のスクロールと重なりを確認する。
    last = child.locator('.wrap').locator(
        'button:visible:enabled, a:visible, input:visible:enabled, select:visible:enabled, textarea:visible:enabled').last
    last.focus()
    expect(last).to_be_focused()
    last.click(trial=True)
    page.keyboard.press('Control+End')
    page.frame(url=f'{web_base_url}/{filename}.html?embed=1').wait_for_function(
        'scrollY + innerHeight >= document.documentElement.scrollHeight - 1')
    box, save = frame.bounding_box(), page.locator('#save-bar').bounding_box()
    assert box['y'] + box['height'] <= save['y']
    page.locator('#save').focus()
    expect(page.locator('#save')).to_be_in_viewport()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_selected_history_and_login_error_text_contrast(page, web_base_url, theme):
    install_api_mocks(page, login_status=401)
    page.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}')")
    page.goto(f'{web_base_url}/chat.html')
    page.locator('[data-open="101"] .cmain').click()
    selected = page.locator('.conv.on')
    expect(selected).to_be_visible()
    colors = selected.evaluate('''(el) => [
        getComputedStyle(el.querySelector('.d')).color, getComputedStyle(el).backgroundColor
    ]''')
    assert _contrast(*colors) >= 4.5
    page.goto(f'{web_base_url}/login.html')
    page.locator('#username').fill('unknown')
    page.locator('#password').fill('bad')
    page.locator('#submit').click()
    expect(page.locator('#err')).to_be_visible()
    colors = page.locator('#err').evaluate('(el) => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]')
    assert _contrast(*colors) >= 4.5


def test_admin_first_iframe_after_theme_switch_uses_current_theme(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    page.locator('#themebtn').click()
    page.locator('[data-tab="users"]').click()
    expect(page.frame_locator('#embed-frame-users').locator('html')).to_have_attribute('data-theme', 'dark')


def test_admin_dirty_state_survives_tabs_and_save_failure(page, web_base_url):
    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    page.locator('#depth-base-max-turns').fill('21')
    expect(page.locator('#tab-dot-provider')).to_be_visible()
    expect(page.locator('#unsaved-note')).to_be_visible()
    page.locator('[data-tab="ingest"]').click()
    page.locator('#arms-list input[data-arm="pdf_text"]').uncheck()
    expect(page.locator('#tab-dot-ingest')).to_be_visible()
    page.locator('[data-tab="provider"]').click()
    page.locator('#depth-base-max-turns').fill('')
    expect(page.locator('#tab-dot-provider')).to_be_hidden()
    page.locator('#depth-base-max-turns').fill('21')

    def reject_save(route):
        assert route.request.method == 'PUT'
        route.fulfill(status=503, json={'detail': '保存に失敗しました（検証用）'})

    page.route('**/admin/settings', reject_save)
    page.locator('#save').click()
    expect(page.locator('#msg')).to_contain_text('保存に失敗')
    expect(page.locator('#depth-base-max-turns')).to_have_value('21')
    expect(page.locator('#tab-dot-provider')).to_be_visible()
    expect(page.locator('#tab-dot-ingest')).to_be_visible()
    page.unroute('**/admin/settings', reject_save)
    page.locator('#save').click()
    expect(page.locator('#msg')).to_contain_text('保存しました')
    assert records['admin_settings_put'][-1]['depth_base_max_turns'] == 21
    assert records['admin_settings_put'][-1]['arms_enabled'] == ['ooxml']
