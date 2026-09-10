import json
import pricing

data = json.load(open(r'data/b2c_data.json', encoding='utf-8'))
cards = data['price_cards']

sfx = cards['shadowfax']['rates']
vel = cards['velocity']['rates']
print('shadowfax keys:', sorted(sfx))
print('velocity keys:', sorted(vel))

# direct unit check of the LM code path
r = pricing.price_shadowfax('Only LM', 1.0, 'Fwd', '<4L', cards['shadowfax'])
print('shadowfax Only LM:       ', r)
r = pricing.price_shadowfax('Kerala (Only LM)', 1.0, 'Fwd', '<4L', cards['shadowfax'])
print('shadowfax Kerala (Only): ', r)
r = pricing.price_velocity('Only LM', 1.0, 'Fwd', '', cards['velocity'])
print('velocity Only LM:        ', r)

# case-insensitive match robustness
r = pricing.price_shadowfax('only lm', 1.0, 'Fwd', '<4L', cards['shadowfax'])
print('shadowfax "only lm" (case):', r)