#!/usr/bin/env node
import Anthropic from '@anthropic-ai/sdk';
import { HttpsProxyAgent } from 'https-proxy-agent';

const payload = JSON.parse(process.argv[2]);

const clientOpts = {};
if (process.env.HTTPS_PROXY) {
  clientOpts.httpAgent = new HttpsProxyAgent(process.env.HTTPS_PROXY);
}

const client = new Anthropic(clientOpts);
const response = await client.messages.create(payload);
process.stdout.write(response.content[0].text);
