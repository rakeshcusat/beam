/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.beam.runners.spark.translation;

import java.io.IOException;
import java.io.Serializable;
import java.util.EnumMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.stream.Collectors;
import javax.annotation.Nullable;
import org.apache.beam.model.fnexecution.v1.BeamFnApi.ProcessBundleProgressResponse;
import org.apache.beam.model.fnexecution.v1.BeamFnApi.ProcessBundleResponse;
import org.apache.beam.model.fnexecution.v1.BeamFnApi.StateKey;
import org.apache.beam.model.fnexecution.v1.BeamFnApi.StateKey.TypeCase;
import org.apache.beam.model.pipeline.v1.RunnerApi;
import org.apache.beam.runners.core.InMemoryTimerInternals;
import org.apache.beam.runners.core.TimerInternals;
import org.apache.beam.runners.core.construction.graph.ExecutableStage;
import org.apache.beam.runners.core.metrics.MetricsContainerImpl;
import org.apache.beam.runners.fnexecution.control.BundleProgressHandler;
import org.apache.beam.runners.fnexecution.control.DefaultJobBundleFactory;
import org.apache.beam.runners.fnexecution.control.JobBundleFactory;
import org.apache.beam.runners.fnexecution.control.OutputReceiverFactory;
import org.apache.beam.runners.fnexecution.control.ProcessBundleDescriptors;
import org.apache.beam.runners.fnexecution.control.RemoteBundle;
import org.apache.beam.runners.fnexecution.control.StageBundleFactory;
import org.apache.beam.runners.fnexecution.control.TimerReceiverFactory;
import org.apache.beam.runners.fnexecution.provisioning.JobInfo;
import org.apache.beam.runners.fnexecution.state.InMemoryBagUserStateFactory;
import org.apache.beam.runners.fnexecution.state.StateRequestHandler;
import org.apache.beam.runners.fnexecution.state.StateRequestHandlers;
import org.apache.beam.runners.fnexecution.translation.BatchSideInputHandlerFactory;
import org.apache.beam.runners.fnexecution.translation.PipelineTranslatorUtils;
import org.apache.beam.runners.spark.coders.CoderHelpers;
import org.apache.beam.runners.spark.metrics.MetricsContainerStepMapAccumulator;
import org.apache.beam.runners.spark.util.ByteArray;
import org.apache.beam.sdk.coders.Coder;
import org.apache.beam.sdk.fn.data.FnDataReceiver;
import org.apache.beam.sdk.transforms.join.RawUnionValue;
import org.apache.beam.sdk.transforms.windowing.BoundedWindow;
import org.apache.beam.sdk.util.WindowedValue;
import org.apache.beam.sdk.util.WindowedValue.WindowedValueCoder;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.vendor.guava.v20_0.com.google.common.base.Preconditions;
import org.apache.spark.api.java.function.FlatMapFunction;
import org.apache.spark.broadcast.Broadcast;
import org.joda.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import scala.Tuple2;

/**
 * Spark function that passes its input through an SDK-executed {@link
 * org.apache.beam.runners.core.construction.graph.ExecutableStage}.
 *
 * <p>The output of this operation is a multiplexed {@link Dataset} whose elements are tagged with a
 * union coder. The coder's tags are determined by {@link SparkExecutableStageFunction#outputMap}.
 * The resulting data set should be further processed by a {@link
 * SparkExecutableStageExtractionFunction}.
 */
class SparkExecutableStageFunction<InputT, SideInputT>
    implements FlatMapFunction<Iterator<WindowedValue<InputT>>, RawUnionValue> {

  private static final Logger LOG = LoggerFactory.getLogger(SparkExecutableStageFunction.class);

  private final RunnerApi.ExecutableStagePayload stagePayload;
  private final Map<String, Integer> outputMap;
  private final JobBundleFactoryCreator jobBundleFactoryCreator;
  // map from pCollection id to tuple of serialized bytes and coder to decode the bytes
  private final Map<String, Tuple2<Broadcast<List<byte[]>>, WindowedValueCoder<SideInputT>>>
      sideInputs;
  private final MetricsContainerStepMapAccumulator metricsAccumulator;
  private final Coder windowCoder;

  private transient InMemoryBagUserStateFactory bagUserStateHandlerFactory;
  private transient Object currentTimerKey;

  SparkExecutableStageFunction(
      RunnerApi.ExecutableStagePayload stagePayload,
      JobInfo jobInfo,
      Map<String, Integer> outputMap,
      Map<String, Tuple2<Broadcast<List<byte[]>>, WindowedValueCoder<SideInputT>>> sideInputs,
      MetricsContainerStepMapAccumulator metricsAccumulator,
      Coder windowCoder) {
    this(
        stagePayload,
        outputMap,
        () -> DefaultJobBundleFactory.create(jobInfo),
        sideInputs,
        metricsAccumulator,
        windowCoder);
  }

  SparkExecutableStageFunction(
      RunnerApi.ExecutableStagePayload stagePayload,
      Map<String, Integer> outputMap,
      JobBundleFactoryCreator jobBundleFactoryCreator,
      Map<String, Tuple2<Broadcast<List<byte[]>>, WindowedValueCoder<SideInputT>>> sideInputs,
      MetricsContainerStepMapAccumulator metricsAccumulator,
      Coder windowCoder) {
    this.stagePayload = stagePayload;
    this.outputMap = outputMap;
    this.jobBundleFactoryCreator = jobBundleFactoryCreator;
    this.sideInputs = sideInputs;
    this.metricsAccumulator = metricsAccumulator;
    this.windowCoder = windowCoder;
  }

  /** Call the executable stage function on the values of a PairRDD, ignoring the key. */
  FlatMapFunction<Tuple2<ByteArray, Iterable<WindowedValue<InputT>>>, RawUnionValue> forPair() {
    return (input) -> call(input._2.iterator());
  }

  @Override
  public Iterator<RawUnionValue> call(Iterator<WindowedValue<InputT>> inputs) throws Exception {
    try (JobBundleFactory jobBundleFactory = jobBundleFactoryCreator.create()) {
      ExecutableStage executableStage = ExecutableStage.fromPayload(stagePayload);
      try (StageBundleFactory stageBundleFactory = jobBundleFactory.forStage(executableStage)) {
        ConcurrentLinkedQueue<RawUnionValue> collector = new ConcurrentLinkedQueue<>();
        StateRequestHandler stateRequestHandler =
            getStateRequestHandler(
                executableStage, stageBundleFactory.getProcessBundleDescriptor());
        if (executableStage.getTimers().size() > 0) {
          // Used with Batch, we know that all the data is available for this key. We can't use the
          // timer manager from the context because it doesn't exist. So we create one and advance
          // time to the end after processing all elements.
          final InMemoryTimerInternals timerInternals = new InMemoryTimerInternals();
          timerInternals.advanceProcessingTime(Instant.now());
          timerInternals.advanceSynchronizedProcessingTime(Instant.now());

          ReceiverFactory receiverFactory =
              new ReceiverFactory(
                  collector,
                  outputMap,
                  new TimerReceiverFactory(
                      stageBundleFactory,
                      (WindowedValue timerElement, TimerInternals.TimerData timerData) -> {
                        currentTimerKey = ((KV) timerElement.getValue()).getKey();
                        timerInternals.setTimer(timerData);
                      },
                      windowCoder));

          // Process inputs.
          processElements(
              executableStage, stateRequestHandler, receiverFactory, stageBundleFactory, inputs);

          // Finish any pending windows by advancing the input watermark to infinity.
          timerInternals.advanceInputWatermark(BoundedWindow.TIMESTAMP_MAX_VALUE);
          // Finally, advance the processing time to infinity to fire any timers.
          timerInternals.advanceProcessingTime(BoundedWindow.TIMESTAMP_MAX_VALUE);
          timerInternals.advanceSynchronizedProcessingTime(BoundedWindow.TIMESTAMP_MAX_VALUE);

          // Now we fire the timers and process elements generated by timers (which may be timers
          // itself)
          try (RemoteBundle bundle =
              stageBundleFactory.getBundle(
                  receiverFactory, stateRequestHandler, getBundleProgressHandler())) {

            PipelineTranslatorUtils.fireEligibleTimers(
                timerInternals,
                (String timerId, WindowedValue timerValue) -> {
                  FnDataReceiver<WindowedValue<?>> fnTimerReceiver =
                      bundle.getInputReceivers().get(timerId);
                  Preconditions.checkNotNull(
                      fnTimerReceiver, "No FnDataReceiver found for %s", timerId);
                  try {
                    fnTimerReceiver.accept(timerValue);
                  } catch (Exception e) {
                    throw new RuntimeException(
                        String.format(Locale.ENGLISH, "Failed to process timer: %s", timerValue));
                  }
                },
                currentTimerKey);
          }
        } else {
          ReceiverFactory receiverFactory = new ReceiverFactory(collector, outputMap);
          processElements(
              executableStage, stateRequestHandler, receiverFactory, stageBundleFactory, inputs);
        }
        return collector.iterator();
      }
    }
  }

  // Processes the inputs of the executable stage. Output is returned via side effects on the
  // receiver.
  private void processElements(
      ExecutableStage executableStage,
      StateRequestHandler stateRequestHandler,
      ReceiverFactory receiverFactory,
      StageBundleFactory stageBundleFactory,
      Iterator<WindowedValue<InputT>> inputs)
      throws Exception {
    try (RemoteBundle bundle =
        stageBundleFactory.getBundle(
            receiverFactory, stateRequestHandler, getBundleProgressHandler())) {
      String inputPCollectionId = executableStage.getInputPCollection().getId();
      FnDataReceiver<WindowedValue<?>> mainReceiver =
          bundle.getInputReceivers().get(inputPCollectionId);
      while (inputs.hasNext()) {
        WindowedValue<InputT> input = inputs.next();
        mainReceiver.accept(input);
      }
    }
  }

  private BundleProgressHandler getBundleProgressHandler() {
    String stageName = stagePayload.getInput();
    MetricsContainerImpl container = metricsAccumulator.value().getContainer(stageName);
    return new BundleProgressHandler() {
      @Override
      public void onProgress(ProcessBundleProgressResponse progress) {
        container.update(progress.getMonitoringInfosList());
      }

      @Override
      public void onCompleted(ProcessBundleResponse response) {
        container.update(response.getMonitoringInfosList());
      }
    };
  }

  private StateRequestHandler getStateRequestHandler(
      ExecutableStage executableStage,
      ProcessBundleDescriptors.ExecutableProcessBundleDescriptor processBundleDescriptor) {
    EnumMap<TypeCase, StateRequestHandler> handlerMap = new EnumMap<>(StateKey.TypeCase.class);
    final StateRequestHandler sideInputHandler;
    StateRequestHandlers.SideInputHandlerFactory sideInputHandlerFactory =
        BatchSideInputHandlerFactory.forStage(
            executableStage,
            new BatchSideInputHandlerFactory.SideInputGetter() {
              @Override
              public <T> List<T> getSideInput(String pCollectionId) {
                Tuple2<Broadcast<List<byte[]>>, WindowedValueCoder<SideInputT>> tuple2 =
                    sideInputs.get(pCollectionId);
                Broadcast<List<byte[]>> broadcast = tuple2._1;
                WindowedValueCoder<SideInputT> coder = tuple2._2;
                return (List<T>)
                    broadcast.value().stream()
                        .map(bytes -> CoderHelpers.fromByteArray(bytes, coder))
                        .collect(Collectors.toList());
              }
            });
    try {
      sideInputHandler =
          StateRequestHandlers.forSideInputHandlerFactory(
              ProcessBundleDescriptors.getSideInputs(executableStage), sideInputHandlerFactory);
    } catch (IOException e) {
      throw new RuntimeException("Failed to setup state handler", e);
    }

    if (bagUserStateHandlerFactory == null) {
      bagUserStateHandlerFactory = new InMemoryBagUserStateFactory();
    }

    final StateRequestHandler userStateHandler;
    if (executableStage.getUserStates().size() > 0) {
      // Need to discard the old key's state
      bagUserStateHandlerFactory.resetForNewKey();
      userStateHandler =
          StateRequestHandlers.forBagUserStateHandlerFactory(
              processBundleDescriptor, bagUserStateHandlerFactory);
    } else {
      userStateHandler = StateRequestHandler.unsupported();
    }

    handlerMap.put(StateKey.TypeCase.MULTIMAP_SIDE_INPUT, sideInputHandler);
    handlerMap.put(StateKey.TypeCase.BAG_USER_STATE, userStateHandler);
    return StateRequestHandlers.delegateBasedUponType(handlerMap);
  }

  interface JobBundleFactoryCreator extends Serializable {
    JobBundleFactory create();
  }

  /**
   * Receiver factory that wraps outgoing elements with the corresponding union tag for a
   * multiplexed PCollection.
   */
  private static class ReceiverFactory implements OutputReceiverFactory {

    private final ConcurrentLinkedQueue<RawUnionValue> collector;
    private final Map<String, Integer> outputMap;
    @Nullable private final TimerReceiverFactory timerReceiverFactory;

    ReceiverFactory(
        ConcurrentLinkedQueue<RawUnionValue> collector, Map<String, Integer> outputMap) {
      this(collector, outputMap, null);
    }

    ReceiverFactory(
        ConcurrentLinkedQueue<RawUnionValue> collector,
        Map<String, Integer> outputMap,
        @Nullable TimerReceiverFactory timerReceiverFactory) {
      this.collector = collector;
      this.outputMap = outputMap;
      this.timerReceiverFactory = timerReceiverFactory;
    }

    @Override
    public <OutputT> FnDataReceiver<OutputT> create(String pCollectionId) {
      Integer unionTag = outputMap.get(pCollectionId);
      if (unionTag != null) {
        int tagInt = unionTag;
        return receivedElement -> collector.add(new RawUnionValue(tagInt, receivedElement));
      } else if (timerReceiverFactory != null) {
        // Delegate to TimerReceiverFactory
        return timerReceiverFactory.create(pCollectionId);
      } else {
        throw new IllegalStateException(
            String.format(Locale.ENGLISH, "Unknown PCollectionId %s", pCollectionId));
      }
    }
  }
}
