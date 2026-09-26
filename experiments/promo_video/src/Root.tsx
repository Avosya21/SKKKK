import React from 'react';
import {Composition} from 'remotion';
import {Video} from './Video';
import {TOTAL_FRAMES} from './timeline';

export const Root: React.FC = () => (
  <Composition id="Promo" component={Video} durationInFrames={TOTAL_FRAMES} fps={30} width={1280} height={720} />
);
