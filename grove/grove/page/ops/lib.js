// What every panel of the Ops page shares: the call to its backend, a loading state around it,
// the number formats, and the classes the cards and list cells agree on.
import { dayjs, frappeRequest } from "frappe-ui";
import { ref } from "vue";

const METHODS = "grove.grove.page.ops.ops.";

export const call = (method, params = {}) =>
	frappeRequest({ url: METHODS + method, method: "POST", params });

/** `load(params)` runs `method`; a slower earlier answer never overwrites a newer one. */
export function useCall(method) {
	const data = ref(null);
	const loading = ref(false);
	const error = ref("");
	let latest = 0;

	async function load(params) {
		const ticket = ++latest;
		loading.value = true;
		error.value = "";
		try {
			const result = await call(method, params);
			if (ticket === latest) data.value = result;
		} catch (failure) {
			if (ticket === latest) error.value = errorText(failure);
		} finally {
			if (ticket === latest) loading.value = false;
		}
	}

	return { data, loading, error, load };
}

export const errorText = (failure) =>
	failure?.messages?.[0] || failure?.message || "The request failed.";

/** Dollars as spent: cents when a dollar or more, else enough digits to read a sub-cent cost. */
export function usd(value) {
	const amount = Number(value ?? 0);
	if (!amount) return "$0";
	if (Math.abs(amount) >= 1) {
		return amount.toLocaleString(undefined, { style: "currency", currency: "USD" });
	}
	const sign = amount < 0 ? "-" : "";
	const digits = Math.abs(amount).toLocaleString(undefined, { maximumSignificantDigits: 3 });
	return `${sign}$${digits}`;
}

export const signedUsd = (value) => (value > 0 ? `+${usd(value)}` : usd(value));

export const compact = (value) =>
	new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(
		Number(value ?? 0)
	);

export const count = (value) => Number(value ?? 0).toLocaleString();

export const percent = (value) => (value == null ? "" : `${Math.round(value)}%`);

export const ago = (value) => (value ? dayjs(value).fromNow() : "never");

export const day = (value) => (value ? dayjs(value).format("MMM D") : "—");

export const when = (value) => (value ? dayjs(value).format("MMM D, HH:mm") : "—");

export const CARD = "rounded-lg border border-outline-gray-2 bg-surface-white p-4";
export const CARD_TITLE = "text-base font-semibold text-ink-gray-8";
export const CELL = "text-sm text-ink-gray-8";
export const NUM = "justify-end text-sm tabular-nums text-ink-gray-8";
export const MUTED = "text-xs text-ink-gray-5";
export const LINK =
	"truncate rounded-sm text-left text-sm text-ink-gray-9 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3";
export const END = "justify-end";
