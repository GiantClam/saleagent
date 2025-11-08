export default {
  async fetch(request, env) {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });
    const sig = request.headers.get('x-signature');
    if (!sig || sig !== env.NOTIFY_TOKEN) return new Response('Unauthorized', { status: 401 });
    const { to, subject, text, html } = await request.json();
    const payload = {
      personalizations: [{ to: [{ email: to }] }],
      from: { email: env.MAIL_FROM, name: env.MAIL_FROM_NAME || 'SaleAgent' },
      subject,
      content: html ? [{ type: 'text/html', value: html }] : [{ type: 'text/plain', value: text || '' }],
      headers: { 'List-Unsubscribe': `<mailto:unsubscribe@${env.MAIL_DOMAIN}>` }
    };
    const r = await fetch('https://api.mailchannels.net/tx/v1/send', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!r.ok) return new Response(await r.text(), { status: r.status });
    return new Response('OK');
  }
}


