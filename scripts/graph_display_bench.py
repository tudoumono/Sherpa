#!/usr/bin/env python3
"""グラフ描画を同一の合成データ＋Chromiumで計測（実API・実データは使わない）。

例: .venv/bin/python scripts/graph_display_bench.py --label after
変更前を比較する場合は --source に graph.html / graph.js / app.css の保存先を指定する。
出力: tmp/graph-display/<label>/metrics.json とスクリーンショット。
readyMs は graph.js 実行開始から配置終了後2フレームまで。実APIの所要時間ではない。
他のブラウザテストと同時に実行せず、同じマシン・ビューポートで比較する。
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ui_shots import _start_static_server
from mock_api import install_api_mocks
from playwright.sync_api import sync_playwright


def graph_data(count):
    types = ['Batch', 'Module', 'Copybook', 'DataItem', 'Table']
    names = ['日次処理', '請求計算', '共通定義', '計算項目', '明細テーブル']
    nodes, edges = [], []
    for i in range(count):
        kind = min(i % 20, 4)
        nodes.append({'id': f'n{i}', 'name': f'{names[kind]} {i:04}', 'type': types[kind],
                      'status': 'active', 'path': f'資料/請求/{i:04}.cbl'})
        if i % 20:
            parent = i - i % 20 + (0 if i % 20 == 1 else 1)
            edges.append({'source': f'n{parent}', 'target': f'n{i}',
                          'type': 'INVOKES' if i % 20 == 1 else 'CONTAINS', 'status': 'active'})
        elif i:
            edges.append({'source': f'n{i - 20}', 'target': f'n{i}', 'type': 'INVOKES', 'status': 'active'})
        if i > 22 and i % 4 == 0:
            edges.append({'source': f'n{i - 19}', 'target': f'n{i}', 'type': 'COPIES', 'status': 'active'})
    return {'nodes': nodes, 'edges': edges, 'counts': {'documents': 50},
            'total_nodes': count, 'total_edges': len(edges), 'truncated': False}


INSTRUMENT = '''
window.graphBench = {started: performance.now()};
const originalCytoscape = window.cytoscape;
window.cytoscape = function(options) {
  const start = performance.now();
  const stop = options.layout.stop;
  options.layout.stop = function(event) {
    graphBench.layoutMs = performance.now() - start;
    if (stop) stop.call(this, event);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      graphBench.readyMs = performance.now() - graphBench.started;
      graphBench.done = true;
    }));
  };
  const result = originalCytoscape(options);
  graphBench.createMs = performance.now() - start;
  window.benchCy = result;
  return result;
};
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', type=Path, default=ROOT / 'web')
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 500, 1500])
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    out = ROOT / 'tmp' / 'graph-display' / args.label
    out.mkdir(exist_ok=True, parents=True)
    server, thread = _start_static_server(0, ROOT / 'web')
    results = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for count in args.sizes:
                for run in range(args.runs):
                    context = browser.new_context(viewport={'width':1440, 'height':1000}, locale='ja-JP')
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.add_init_script("window.longTasks=[]; new PerformanceObserver(l=>longTasks.push(...l.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true})")
                    install_api_mocks(page)
                    page.route(re.compile(r'/graph\?'), lambda route: route.fulfill(json=graph_data(count)))
                    for asset in ['app.css', 'graph.html', 'graph.js']:
                        content = (args.source / asset).read_text()
                        if asset == 'graph.js':
                            content = INSTRUMENT + content
                        page.route('**/' + asset, lambda route, *, content=content, asset=asset: route.fulfill(
                            body=content, content_type='text/css' if asset.endswith('.css') else
                            'text/javascript' if asset.endswith('.js') else 'text/html'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/graph.html')
                    page.wait_for_function('window.graphBench?.done', timeout=60000)
                    metrics = page.evaluate('''async () => {
                      const selectStart = performance.now();
                      benchCy.nodes()[1].emit('tap');
                      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
                      const selectMs = performance.now() - selectStart;
                      const frames=[];
                      for(let i=0;i<20;i++) {
                        const t=performance.now(); benchCy.panBy({x:2,y:1});
                        await new Promise(requestAnimationFrame); frames.push(performance.now()-t);
                      }
                      return {...graphBench, selectMs, panFrameMax:Math.max(...frames),
                        panFrameMean:frames.reduce((a,b)=>a+b)/frames.length,
                        nodes:benchCy.nodes().length, edges:benchCy.edges().length,
                        visibleNodesAfterSelection:benchCy.nodes(':visible').length,
                        maxLongTask:Math.max(0,...longTasks), longTaskMs:longTasks.reduce((a,b)=>a+b,0)};
                    }''')
                    if run == 0:
                        page.screenshot(path=str(out / f'{count}-selected.png'))
                    if run == 0:
                        page.evaluate("benchCy.emit('tap')")
                        page.screenshot(path=str(out / f'{count}-overview.png'))
                    assert not errors, '\n'.join(errors)
                    metrics.update(count=count, run=run, errors=errors)
                    results.append(metrics)
                    (out / 'metrics.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
                    print(json.dumps(metrics, ensure_ascii=False), flush=True)
                    context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
