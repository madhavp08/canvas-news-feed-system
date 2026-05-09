export type NotifyChangeType = "NEW_DATE" | "UPDATED_SAME_DATE";

export type TldrNormalized =
  | { kind: "paragraph"; text: string }
  | { kind: "bullets"; lead: string | null; bullets: string[] };

export type ItemSegment =
  | { type: "text"; text: string }
  | { type: "link"; href: string; label: string };

export type NewsFeedEmailProps = {
  changeType: NotifyChangeType;
  dateRaw: string;
  /** One array of segments per bulletin line */
  items: ItemSegment[][];
  tldr: TldrNormalized | null;
  previewText: string;
  /** Optional promo line from EMAIL_PROMO_TEXT; null to omit the callout */
  promoText: string | null;
  /** First http(s) URL from promo text; used as logo link target */
  promoLinkHref: string | null;
  /** Promo logo image URL when EMAIL_PROMO_TEXT is set (EMAIL_PROMO_LOGO_URL or default) */
  promoLogoUrl: string | null;
  /**
   * When set with promo, email shows “Was this ad helpful?” linking to `{base}/vote?choice=…`.
   * Set AD_FEEDBACK_PUBLIC_URL in `.env`; run `python main.py ad-feedback-serve`.
   */
  adFeedbackBaseUrl?: string | null;
};
