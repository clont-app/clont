# where the dollar figures come from

everything comes out of `clont/finops/aws/prices.json` — public on-demand list
prices, generated offline from the aws price list bulk api. nothing is fetched at
runtime and no iam grant is involved.

instance rates are per *type*, per region, as aws publishes them — ~1400 types
including `.metal`. ebs carries the provisioned iops and gp3 throughput skus too,
so a gp2 -> gp3 saving is net of the performance the gp3 has to buy back.

they're still estimates and clont says so. commitment discounts are flat guesses,
io2's cheaper iops tiers above 32k aren't modelled, and a type launched after the
table was generated falls back to its family's `.large` scaled by size. a resource
in a region that's missing from the table gets priced at us-east-1 rates and
marked approximate, so the report says "estimated at us-east-1 rates" instead of
passing a guess off as a quote.

public ipv4 has its own key (`public_ipv4_hourly`, the in-use sku). it's the same
$0.005/hr as an idle elastic ip today, but they're separate skus and aws can move
one without the other — if your price table predates the in-use sku, clont falls
back to the idle rate instead of the generic default.

**own iron and kubernetes never touch that table.** their numbers come from the
rate card you wrote, divided by the capacity clont measured — your money, not a
list price. that's why a per-cluster on-prem finding is exact where an aws one is
an estimate.

regenerate at release time, prices drift:

```sh
python tools/gen_prices.py          # ~15 min, no credentials needed
```
