export default defineContentScript({
	matches: ['<all_urls>'],
	main() {
		chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
			if (message.command === 'autofill') {
				getInputField();
				sendResponse({ status: 'done' });
			}
		});
	},
});

const storageKeys = ['fname', 'lname', 'email', 'phone', 'linkedin', 'github', 'portfolio'];

function getLabelText(input: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement): string {
	// Same as before, example helper (optional)
	if (input.id) {
		const label = document.querySelector(`label[for="${input.id}"]`);
		if (label) return label.textContent?.trim() ?? '';
	}
	const parentLabel = input.closest('label');
	if (parentLabel) return parentLabel.textContent?.trim() ?? '';
	return '';
}

function getInputField() {
	chrome.storage.local.get(storageKeys, (userData) => {
		const fields = document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
			'input:not([type=hidden]):not([disabled]):not([readonly]), textarea:not([disabled]):not([readonly]), select:not([disabled])'
		);
		fields.forEach((field) => {
			const label = getLabelText(field);
			const placeholder = field.getAttribute('placeholder') || '';
			const ariaLabel = field.getAttribute('aria-label') || '';
			const name = field.getAttribute('name') || '';
			const id = field.id || '';
			const prev = field.previousElementSibling;
			const siblingText = prev instanceof HTMLElement ? prev.innerText.trim() : '';

			const combined = [label, placeholder, ariaLabel, name, id, siblingText]
				.filter(Boolean)
				.join(' ')
				.toLowerCase();

			fillInputField(field, combined, userData);
		});
	});
}

function matchesKeywords(text: string, keywords: string[]) {
	return keywords.some((kw) => text.includes(kw.toLowerCase()));
}

function fillInputField(
	field: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement,
	combined: string,
	userData: Record<string, string>
) {
	if (matchesKeywords(combined, ['email'])) {
		(field as HTMLInputElement).value = userData.email;
	} else if (matchesKeywords(combined, ['phone', 'mobile', 'tel'])) {
		(field as HTMLInputElement).value = userData.phone;
	} else if (matchesKeywords(combined, ['first'])) {
		(field as HTMLInputElement).value = userData.fname;
	} else if (matchesKeywords(combined, ['last'])) {
		(field as HTMLInputElement).value = userData.lname;
	} else if (matchesKeywords(combined, ['name'])) {
		(field as HTMLInputElement).value = userData.fname + ' ' + userData.lname;
	} else if (matchesKeywords(combined, ['address', 'street', 'city', 'zip', 'postal'])) {
		(field as HTMLInputElement).value = userData.address;
	} else if (matchesKeywords(combined, ['portfolio'])) {
		(field as HTMLInputElement).value = userData.portfolio;
	} else if (matchesKeywords(combined, ['linkedin'])) {
		(field as HTMLInputElement).value = userData.linkedin;
	} else if (matchesKeywords(combined, ['github'])) {
		(field as HTMLInputElement).value = userData.github;
	}
	// If it's a <select>, try to select the first option if no match above
	if (field.tagName.toLowerCase() === 'select') {
		const select = field as HTMLSelectElement;
		if (select.options.length > 0 && !select.value) {
			select.value = select.options[0].value;
		}
	}
}
