#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright
from config import DEFAULT_INPUT
from storage import setup_logging, load_clients, load_state
from browser_utils import launch_context
from navigation import wait_for_user_login
from workflow import interactive_loop
from debug_bundle import cleanup_old_diagnostics

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument('--input',type=Path,default=DEFAULT_INPUT)
    p.add_argument('--commit',action='store_true')
    p.add_argument('--start-client', help='Client name, optionally followed by | invoice number')
    p.add_argument('--batch-size',type=int,default=5)
    args=p.parse_args()
    setup_logging(); cleanup_old_diagnostics(); clients=load_clients(args.input); state=load_state()
    start=0
    if args.start_client:
        target=args.start_client.strip()
        if "|" in target:
            target_name,target_invoice=[part.strip() for part in target.split("|",1)]
            matches=[i for i,c in enumerate(clients) if c.client==target_name and c.invoice_number==target_invoice]
        else:
            matches=[i for i,c in enumerate(clients) if c.client==target]
        if not matches:
            raise ValueError(f'Start payment run not found: {args.start_client}')
        if len(matches)>1:
            raise ValueError('Multiple invoice groups found for that client. Use --start-client "CLIENT | INVOICE".')
        start=matches[0]
    print('\nMODE:', 'COMMIT MODE' if args.commit else 'DRY-RUN MODE')
    with sync_playwright() as pw:
        context=launch_context(pw)
        try:
            wait_for_user_login(context)
            interactive_loop(context,clients,state,args.commit,start,args.batch_size,args.input)
        finally:
            context.close()
    return 0

if __name__=='__main__': raise SystemExit(main())
