# AC information in filenames

Frequency, `LIA` input sensitivity, and `Pre` preamplifier sensitivity retain
their existing tags. `LIA200mV` is input sensitivity, not AC excitation.

- `ACout20mV`: 20 mV Sine Out setting from an instrument snapshot.
- `ACset20mV`: displayed/saved 20 mV Sine Out, not instrument verified.
- `ACMoTe2E2`: optional contact entered in Signal Chain / AC contact.

Contact tags follow the custom description and precede scan parameters:
`YZ324_1.67K0T_ACMoTe2E2_Vds0.8to1.3V_17Hz_LIA200mV_Pre10nA_ACout20mV_TIMESTAMP.csv`.
If that exact contact already occurs in the custom description, its position
is preserved and no extra copy is appended. AC amplitude remains with the
signal-chain settings near the end of the name.

Ordinary scan starts use the existing verified run snapshot for path creation.
Saved previews and preplanned field-batch base names can retain `ACset`; the run
metadata records the actual captured instrument settings and their provenance.
No extra hardware reads are performed for filename previews. Missing or invalid
amplitudes do not produce invented numeric tags. Sine Out is an instrument
setting, not a measurement of sample voltage.

## Optional user-estimated AC voltage ratio

Signal Chain / AC voltage ratio (estimate) accepts the user's estimate of
sample-side voltage divided by Lock-in output voltage. It starts unspecified;
no 0.22 or unity default is assumed. The entered value is remembered. Set zero
to return to Not specified. The ratio supports both attenuation and step-up.

With ratio 0.22 and Sine Out 20 mV, the display estimates 4.4 mV and filenames
add `VacEst4.4mV` after `ACout20mV` or `ACset20mV`. The latter tag still indicates
whether the source setting was instrument-verified. This is always an estimate,
not a calibrated or measured sample voltage. Use the same amplitude convention
on both sides of the ratio; frequency/load dependence is not compensated.

Metadata records `ac_voltage_ratio`, its definition, source `user estimate`, and
`sample_ac_voltage_estimate_v`. Without a ratio or valid amplitude, the estimate
is absent from filenames and null in metadata. The ratio affects only estimates
and recording; current conversion, instrument outputs and limits are unchanged.

Exact structured tags already in the free-form name are included once. Existing
ACout/ACset amplitude tags are replaced when a new structured amplitude exists.
Other descriptions and manual wiring labels remain intact. Existing data files
are not renamed.
