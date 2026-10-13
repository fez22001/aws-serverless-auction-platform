// Local Velocity rendering with the API Gateway helpers used by our templates.
// This does not emulate deployment integration, IAM, or GatewayResponse behavior.
const { render } = require('velocityjs');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = { responseOverride: {}, requestId: 'gateway-request', authorizer: { claims: { sub: input.principal || 'signed-in-user' } } };
const body = JSON.parse(input.body);
const field = p => body[p.slice(2)] ?? null;
const rendered = render(fs.readFileSync(input.path, 'utf8'), {
  input: { body: input.body, path: field, json: p => JSON.stringify(field(p)) }, context,
  stageVariables: { BiddingStateMachineArn: 'arn:local:bidding', CloseAuctionStateMachineArn: 'arn:local:close' },
  util: {
    parseJson: JSON.parse,
    escapeJavaScript(value) {
      const escaped = JSON.stringify(value).slice(1, -1).replace(/'/g, "\\'");
      // API Gateway uses Java String.replaceAll (regex), not JS literal replaceAll.
      const result = new String(escaped);
      result.replaceAll = (pattern, replacement) => escaped.replace(new RegExp(pattern, 'g'), replacement);
      return result;
    },
  },
});
process.stdout.write(JSON.stringify({ rendered, status: context.responseOverride.status || 200 }));
