# Brand images

Home Assistant and HACS load integration icons from
<https://brands.home-assistant.io>, not from the integration itself. For a
custom integration the images are submitted to the
[home-assistant/brands](https://github.com/home-assistant/brands) repository
under `custom_integrations/<domain>/`.

`custom_integrations/falbygdens_energi/` here holds the files ready to submit:

| File | Size | Rule |
| --- | --- | --- |
| `icon.png` | 256 × 256 | square, transparent background |
| `icon@2x.png` | 512 × 512 | same at 2× |
| `logo.png` | 256 × 80 | longest side 256 |
| `logo@2x.png` | 512 × 160 | longest side 512 |

The icon is the "Fe" roundel from Falbygdens Energi's logotype, the logo the
full logotype as shown on the customer portal. Both are trademarks of
Falbygdens Energi and used only to identify the service, as the brands
repository requires.

## Submitting

```bash
gh repo fork home-assistant/brands --clone
cd brands
mkdir -p custom_integrations/falbygdens_energi
cp ../HA-Falbygdensenergi/brands/custom_integrations/falbygdens_energi/*.png custom_integrations/falbygdens_energi/
git checkout -b add-falbygdens-energi
git add custom_integrations/falbygdens_energi
git commit -m "Add falbygdens_energi custom integration"
gh pr create --fill
```

The images appear at `https://brands.home-assistant.io/_/falbygdens_energi/icon.png`
once the pull request is merged; Home Assistant and HACS pick them up
automatically after that, no release needed.
