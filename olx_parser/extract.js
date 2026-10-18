// Read only rendered public fields. No network calls, clicks or page-state APIs.
// Selector candidates are configurable; see README for live verification limits.
({selectors, offerPatterns, businessPatterns}) => {
  const visible = element => {
    if (!element || !element.isConnected || !element.getClientRects().length) return false;
    const style = getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const first = selector => {
    // Prefer the explicit field selectors to generic headings, independent of DOM order.
    for (const part of selector.split(',')) {
      const match = all(part.trim())[0];
      if (match) return match;
    }
    return null;
  };
  const text = element => element && typeof element.innerText === 'string'
    ? element.innerText.trim() : '';
  const norm = value => value.toLocaleLowerCase('pt-PT').normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '');
  const title = text(first(selectors.title));
  const price = text(first(selectors.price));
  const parameters = first(selectors.parameters);
  const description = first(selectors.description);
  const sellers = all(selectors.seller);
  const contacts = all(selectors.contact);
  const types = [];
  if (parameters) {
    for (const element of [parameters, ...parameters.querySelectorAll('*')]) {
      if (visible(element) && /^(Particular|Profissional|Empresa|Negócio)$/iu.test(text(element))) {
        types.push(text(element));
      }
    }
  }
  const registrationLabels = [];
  const since = /No OLX desde\s+(?:\d{1,2}\s+de\s+)?[\p{L}]+\s+de\s+\d{4}/giu;
  for (const seller of sellers) {
    // Return only the label; never the seller's name, profile link or identifiers.
    registrationLabels.push(...text(seller).matchAll(since));
  }
  let phoneConfirmed = false;
  for (const scope of [...sellers, ...contacts]) {
    for (const anchor of scope.querySelectorAll('a[href^="tel:"]')) {
      if (!visible(anchor) || anchor.getAttribute('aria-disabled') === 'true') continue;
      // Digits remain in the browser expression. Python receives only true/false.
      const digits = (anchor.getAttribute('href') || '').slice(4).replace(/[\s().+-]/g, '');
      if (/^(?:351)?[29]\d{8}$/.test(digits)) phoneConfirmed = true;
    }
  }
  const categoryUrls = all(selectors.breadcrumbs).map(a => a.href).filter(Boolean);
  const collectBreadcrumbs = node => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(collectBreadcrumbs); return; }
    const types = Array.isArray(node['@type']) ? node['@type'] : [node['@type']];
    if (types.includes('BreadcrumbList')) {
      for (const entry of node.itemListElement || []) {
        const item = entry.item;
        const url = typeof item === 'string' ? item : item?.['@id'] || item?.url;
        if (typeof url === 'string') categoryUrls.push(url);
      }
    }
    if (node['@graph']) collectBreadcrumbs(node['@graph']);
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { collectBreadcrumbs(JSON.parse(script.textContent)); } catch { /* not evidence */ }
  }
  const offerText = norm(title + '\n' + text(description));
  const businessText = offerText + '\n' + sellers.map(s => norm(text(s))).join('\n');
  return {
    title, price,
    seller_types: [...new Set(types)],
    registration_labels: [...new Set(registrationLabels.map(match => match[0]))],
    phone_confirmed: phoneConfirmed,
    category_urls: [...new Set(categoryUrls)],
    description_checked: Boolean(description && text(description)),
    excluded_offer: offerPatterns.some(pattern => new RegExp(pattern, 'iu').test(offerText)),
    business_signal: businessPatterns.some(pattern => new RegExp(pattern, 'iu').test(businessText))
  };
}

