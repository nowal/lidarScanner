import React, {useEffect, useState} from 'react';
import {Button, NativeEventEmitter, NativeModules, Text, View} from 'react-native';

const {SpatialCaptureBridge} = NativeModules;
const emitter = new NativeEventEmitter(SpatialCaptureBridge);

type CaptureState = {
  appState: string;
  meshCount: number;
  hasRoom: boolean;
};

export default function SpatialCaptureScreen() {
  const [state, setState] = useState<CaptureState>({
    appState: 'idle',
    meshCount: 0,
    hasRoom: false,
  });

  useEffect(() => {
    SpatialCaptureBridge.getState().then(setState).catch(() => undefined);
    const sub = emitter.addListener('spatialCaptureState', (next: CaptureState) => setState(next));
    return () => sub.remove();
  }, []);

  return (
    <View style={{flex: 1, justifyContent: 'center', padding: 24, gap: 12}}>
      <Text>State: {state.appState}</Text>
      <Text>Mesh anchors: {state.meshCount}</Text>
      <Text>Room captured: {state.hasRoom ? 'yes' : 'no'}</Text>
      <Button title="Start Scan" onPress={() => SpatialCaptureBridge.startScan()} />
      <Button title="Stop Scan" onPress={() => SpatialCaptureBridge.stopScan()} />
      <Button title="Reset" onPress={() => SpatialCaptureBridge.reset()} />
    </View>
  );
}
