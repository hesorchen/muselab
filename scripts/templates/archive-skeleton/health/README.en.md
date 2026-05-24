# health/ — body / health

Everyone has a body — regardless of age, occupation, or life stage.
This directory holds **everything about that body**.

## What goes here

- **Checkup reports**: each PDF / report. Name them `YYYY-MM-checkup-clinic.pdf`
- **Current medications / supplements**: dose, brand, start date, why
- **Exercise / training logs**: current weekly plan / historical progress
- **Sleep / heart rate / steps**: optional exports (Apple Health / Garmin
  / Fitbit / Mi Band etc.)
- **Clinical records**: prescriptions, imaging, doctor notes, follow-up plans
- **Allergies / chronic conditions**: anything that follows you long-term
- **Health of people you care about** (optional): parents / kids /
  partner — make per-person subdirectories

## What this looks like at different life stages

| Your situation | Roughly what's in here |
|----------------|------------------------|
| Student / young | School physicals / vision / fitness / occasional sports injuries |
| TTC / pregnancy / postpartum | Prenatal reports / nutrition logs / postpartum recovery |
| Mid-life | Annual checkups / long-term metric trends / chronic-condition management / training adjustments |
| Sandwich generation | Yourself + parents on parallel tracks (parents in `parents/` subdir) |
| Pre-retirement / older | Chronic management / medication table / fall prevention / spouse's health |
| Chronic / recovery | Follow-up plans / medication table / symptom journal |
| Mental-health focused | Therapy notes / sleep journal / mood logs (encrypt separately if you prefer) |

## What Muse can do with this

- Cross-year checkup comparison: which metrics are improving /
  worsening / approaching critical
- Supplement review: doses too high / drug interactions / better
  alternatives
- Training plan adjustment: based on recent data + your goals
- Pre-checkup prep: which tests to ask the clinic to add
- When abnormal numbers show up: evidence-based next step (which
  specialty to see / what to test)
- Parent / elder health tracking: at which stage to screen for what

## Notes

- Keep the **original numbers** — Muse will quote specific values
- Get units right (mmol/L vs mg/dL; ng/mL vs nmol/L are very different)
- Redact medical record numbers / IDs before saving
- Mental health / sexual health / psychiatric medication: consider a
  separate encrypted file — your comfort level

## What Muse will NOT do (important guardrails)

- No diagnoses
- No prescriptions
- No supplement-effect exaggeration (keeps the line vs medication clear)
- No MLM brand pushes (no Amway / Herbalife / similar, absent
  independent evidence)
- Always reminds you to **see a doctor** for important decisions

## Getting started

1. Drop in your most recent checkup report (if you're a student with no
   recent checkup, even "height X, weight Y, no major issues" is enough)
2. List **everything you're currently taking** (meds + supplements)
3. Write a single line about **your top health concern right now** (or
   "no specific concerns")

The first time Muse comes in, it'll proactively ask "want me to read the
checkup first, or talk about exercise?"
