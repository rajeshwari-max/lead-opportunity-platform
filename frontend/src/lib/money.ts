/** Approximate conversion of scraped funding amounts into INR.
 *
 *  `funding_amount` is free text exactly as the funder wrote it — "$905,664 –
 *  $1,188,684", "up to £250,000", "USD 75,000 – USD 400,000", "$25 million".
 *  There is no structured currency field to convert, so this parses the text.
 *
 *  Everything here is indicative only. Rates are fixed, not live, so a figure
 *  shown in INR is a rough sense of scale for triage — never a number to put in
 *  a budget or a proposal. That is why the UI always shows the funder's own
 *  wording alongside and prefixes the conversion with "≈".
 */

/** Units of one INR. Update RATES_AS_OF whenever these are refreshed. */
export const RATES_AS_OF = "August 2026";

const RATES: Record<string, number> = {
  INR: 1,
  USD: 87.5,
  EUR: 95.0,
  GBP: 111.0,
  AUD: 57.0,
  CAD: 63.0,
  CHF: 99.0,
  SGD: 65.0,
  JPY: 0.58,
  ZAR: 4.8,
  KES: 0.68,
  SEK: 8.4,
  NOK: 8.2,
  DKK: 12.7,
};

/** Longest-first so "US$" is tested before "$" and CA$ isn't read as USD. */
const CURRENCY_TOKENS: [RegExp, string][] = [
  [/\b(?:INR|Rs\.?|₹)/i, "INR"],
  [/\b(?:AUD|A\$|AU\$)/i, "AUD"],
  [/\b(?:CAD|CA\$|C\$)/i, "CAD"],
  [/\b(?:SGD|S\$)/i, "SGD"],
  [/\b(?:CHF)\b/i, "CHF"],
  [/\b(?:ZAR)\b/i, "ZAR"],
  [/\b(?:KES)\b/i, "KES"],
  [/\b(?:SEK)\b/i, "SEK"],
  [/\b(?:NOK)\b/i, "NOK"],
  [/\b(?:DKK)\b/i, "DKK"],
  [/\b(?:JPY)|¥/i, "JPY"],
  [/\b(?:GBP)|£/i, "GBP"],
  [/\b(?:EUR)|€/i, "EUR"],
  [/\b(?:USD|US\$)/i, "USD"],
  [/\$/, "USD"], // bare $ last: most funders here mean USD
];

function detectCurrency(text: string): string | null {
  for (const [re, code] of CURRENCY_TOKENS) {
    if (re.test(text)) return code;
  }
  return null;
}

/** "1.5 million" -> 1500000. Handles m / mn / bn / k / lakh / crore. */
const MULTIPLIERS: [RegExp, number][] = [
  [/^\s*(?:crores?|cr)\b/i, 1e7],
  [/^\s*(?:lakhs?|lacs?)\b/i, 1e5],
  [/^\s*(?:billion|bn|b)\b/i, 1e9],
  [/^\s*(?:million|mn|m)\b/i, 1e6],
  [/^\s*(?:thousand|k)\b/i, 1e3],
];

function amountsIn(text: string): number[] {
  const out: number[] = [];
  // A number with optional thousands separators and decimals.
  const re = /(\d[\d,]*(?:\.\d+)?)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const n = Number(m[1].replace(/,/g, ""));
    if (!Number.isFinite(n) || n === 0) continue;
    const rest = text.slice(re.lastIndex);
    let value = n;
    for (const [mre, factor] of MULTIPLIERS) {
      if (mre.test(rest)) {
        value = n * factor;
        break;
      }
    }
    out.push(value);
  }
  return out;
}

/** Indian grouping: ₹1.2 Cr, ₹45 L, ₹8,500. */
export function formatInr(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value >= 1e7) {
    const cr = value / 1e7;
    return `₹${cr >= 100 ? Math.round(cr) : cr.toFixed(cr >= 10 ? 0 : 1)} Cr`;
  }
  if (value >= 1e5) {
    const l = value / 1e5;
    return `₹${l >= 10 ? Math.round(l) : l.toFixed(1)} L`;
  }
  return `₹${Math.round(value).toLocaleString("en-IN")}`;
}

/**
 * Convert a scraped amount string to an approximate INR range.
 * Returns "" when the text has no currency or no usable number — a silent
 * skip is right here, because guessing a currency would produce a confidently
 * wrong number, which is worse than showing nothing.
 */
export function toInr(text: string | null | undefined): string {
  const raw = (text || "").trim();
  if (!raw) return "";

  const code = detectCurrency(raw);
  if (!code) return "";
  const rate = RATES[code];
  if (!rate) return "";
  if (code === "INR") return ""; // already INR — nothing to add

  const values = amountsIn(raw);
  if (values.length === 0) return "";

  const converted = values.map((v) => v * rate);
  const lo = Math.min(...converted);
  const hi = Math.max(...converted);

  // Ranges are common ("£10,000 – £150,000"); collapse when both ends round
  // to the same display value so it doesn't read "₹1.1 Cr – ₹1.1 Cr".
  const loText = formatInr(lo);
  const hiText = formatInr(hi);
  return loText === hiText ? `≈ ${loText}` : `≈ ${loText} – ${hiText}`;
}
