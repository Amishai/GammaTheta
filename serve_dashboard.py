#!/usr/bin/env python3
"""
serve_dashboard.py — Persistent local server for FX Options Analytics

Usage:
    python serve_dashboard.py                  # Serve on port 8080
    python serve_dashboard.py --port 9090      # Custom port
    python serve_dashboard.py --refresh 300    # Re-generate from Excel every 5 min
    python serve_dashboard.py --no-browser     # Don't auto-open browser

How it works:
  - Serves fx_gamma_trading.html and static files from the working directory
  - Proxies /api/optionflow requests to dtcc.ericlanalytics.com (avoids CORS)
  - The DTCC tab auto-polls every 30s via the proxy
  - Optionally re-generates the dashboard from Excel on a schedule
"""

import argparse
import os
import sys
import json
import threading
import time
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler

DTCC_UPSTREAM = 'https://dtcc.ericlanalytics.com'


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves static files + proxies /api/ requests to the DTCC upstream."""

    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith('/api/'):
            self._proxy_request()
        else:
            super().do_GET()

    def _proxy_request(self):
        upstream_url = DTCC_UPSTREAM + self.path
        try:
            req = urllib.request.Request(upstream_url, headers={
                'User-Agent': 'FXOptionsAnalytics/1.0',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            self.send_error(e.code, f'Upstream error: {e.reason}')
        except urllib.error.URLError as e:
            self.send_error(502, f'Cannot reach DTCC upstream: {e.reason}')
        except Exception as e:
            self.send_error(500, f'Proxy error: {str(e)}')

    def log_message(self, format, *args):
        # args vary by caller: normal requests give (request, status_code, size)
        # send_error gives (code, message) — don't crash on either
        try:
            status = int(args[1]) if len(args) > 1 else 0
        except (ValueError, TypeError):
            status = 0
        if self.path.startswith('/api/') or status >= 400:
            sys.stderr.write(f"  [{time.strftime('%H:%M:%S')}] {self.path} -> {args[1] if len(args)>1 else ''}\n")


def regenerate_dashboard():
    """Re-run the main script to rebuild the HTML from Excel data."""
    try:
        import importlib
        import fx_gamma_richness as fxg
        importlib.reload(fxg)

        if os.path.exists('fx_gamma_inputs.xlsx'):
            positions = fxg.load_positions('fx_gamma_inputs.xlsx')
            fxg.create_dashboard(positions)
            print(f"  [refresh] Dashboard regenerated at {time.strftime('%H:%M:%S')}")
        else:
            print("  [refresh] fx_gamma_inputs.xlsx not found, skipping")
    except Exception as e:
        print(f"  [refresh] Error regenerating dashboard: {e}")


def refresh_loop(interval):
    """Background thread that regenerates the dashboard on a schedule."""
    while True:
        time.sleep(interval)
        regenerate_dashboard()


def main():
    parser = argparse.ArgumentParser(description='Serve FX Options Analytics dashboard')
    parser.add_argument('--port', type=int, default=8080, help='Port (default 8080)')
    parser.add_argument('--refresh', type=int, default=0,
                        help='Auto-regenerate from Excel every N seconds (0=off)')
    parser.add_argument('--no-browser', action='store_true', help='Do not auto-open browser')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    if not os.path.exists('fx_gamma_trading.html'):
        print("Dashboard HTML not found, generating from Excel...")
        regenerate_dashboard()
        if not os.path.exists('fx_gamma_trading.html'):
            print("ERROR: Could not generate dashboard. Run: python fx_gamma_richness.py")
            sys.exit(1)

    if args.refresh > 0:
        print(f"  Auto-refresh: re-generating from Excel every {args.refresh}s")
        t = threading.Thread(target=refresh_loop, args=(args.refresh,), daemon=True)
        t.start()

    url = f'http://localhost:{args.port}/fx_gamma_trading.html'

    print()
    print("=" * 55)
    print("  FX OPTIONS ANALYTICS — DASHBOARD SERVER")
    print("=" * 55)
    print()
    print(f"  Dashboard:  {url}")
    print(f"  DTCC proxy: http://localhost:{args.port}/api/optionflow -> {DTCC_UPSTREAM}")
    print(f"  Excel auto: {'every ' + str(args.refresh) + 's' if args.refresh > 0 else 'off (use --refresh N)'}")
    print()
    print("  Press Ctrl+C to stop")
    print()

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    server = HTTPServer(('', args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == '__main__':
    main()
