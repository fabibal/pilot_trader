"""Explicit direction and conservative handling of ambiguous legacy signals."""
import re


def semantics(event):
    side = event.get('side')
    action = event.get('position_action')
    state = event.get('entry_status')
    if side in ('long', 'short') and action in ('open', 'add', 'reduce', 'close', 'hold'):
        return side, action, state if state in ('confirmed', 'setup') else 'review'
    old = event.get('signal_type')
    text = event.get('text') or ''
    # Legacy sell mixes shorts and exits. Do not infer a filled short from prose.
    if old == 'sell' and re.search(r'\b(short|shorting|shorted|cover|covered|covering)\b', text, re.I):
        return 'short', 'open', 'setup' if re.search(r'\b(potential|watching|setup)\b', text, re.I) else 'review'
    action = {'buy': 'open', 'sell': 'reduce' if event.get('sell_kind') == 'partial' else 'close',
              'position': 'hold'}.get(old)
    if old == 'buy' and re.search(r'\b(potential|watching|watchlist)\b', text, re.I):
        return 'long', 'open', 'review'
    return 'long', action, 'legacy'
