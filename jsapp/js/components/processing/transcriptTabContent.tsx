import React from 'react';
import clonedeep from 'lodash.clonedeep';
import envStore from 'js/envStore';
import bem from 'js/bem';
import {formatTime} from 'js/utils';
import type {AnyRowTypeName} from 'js/constants';
import singleProcessingStore from 'js/components/processing/singleProcessingStore';
import LanguageSelector from 'js/components/languages/languageSelector';
import languageSelectorActions from 'js/components/languages/languageSelectorActions';
import Button from 'js/components/common/button';
import 'js/components/processing/processingBody';
import {destroyConfirm} from 'js/alertify';

interface TranscriptTabContentProps {
  questionType: AnyRowTypeName | undefined;
}

export default class TranscriptTabContent extends React.Component<
  TranscriptTabContentProps
> {
  private unlisteners: Function[] = [];

  componentDidMount() {
    this.unlisteners.push(
      singleProcessingStore.listen(this.onSingleProcessingStoreChange, this)
    );
  }

  componentWillUnmount() {
    this.unlisteners.forEach((clb) => {clb();});
  }

  /**
  * Don't want to store a duplicate of store data here just for the sake of
  * comparison, so we need to make the component re-render itself when the
  * store changes :shrug:.
  */
  onSingleProcessingStoreChange() {
    this.forceUpdate();
  }

  /** Changes the draft language, preserving the other draft properties. */
  onLanguageChange(newVal: string | undefined) {
    const newDraft = clonedeep(singleProcessingStore.getTranscriptDraft()) || {};
    newDraft.languageCode = newVal;
    singleProcessingStore.setTranscriptDraft(newDraft);
  }

  /** Changes the draft value, preserving the other draft properties. */
  setDraftValue(newVal: string | undefined) {
    const newDraft = clonedeep(singleProcessingStore.getTranscriptDraft()) || {};
    newDraft.value = newVal;
    singleProcessingStore.setTranscriptDraft(newDraft);
  }

  onDraftValueChange(evt: React.ChangeEvent<HTMLTextAreaElement>) {
    this.setDraftValue(evt.target.value);
  }

  begin() {
    // Make an empty draft.
    singleProcessingStore.setTranscriptDraft({});
  }

  selectModeManual() {
    // Initialize draft value.
    this.setDraftValue('');
  }

  selectModeAuto() {
    // TODO: this will display an automated service selector that will
    // ultimately produce a draft value.
  }

  back() {
    const draft = singleProcessingStore.getTranscriptDraft();
    if (
      draft !== undefined &&
      draft?.languageCode === undefined &&
      draft?.value === undefined
    ) {
      this.discardDraft();
    }

    if (
      draft !== undefined &&
      draft?.languageCode !== undefined &&
      draft?.value === undefined
    ) {
      singleProcessingStore.setTranslationDraft({});
      languageSelectorActions.resetAll();
    }
  }

  discardDraft() {
    if (singleProcessingStore.hasUnsavedTranscriptDraftValue()) {
      destroyConfirm(
        singleProcessingStore.deleteTranscriptDraft.bind(singleProcessingStore),
        t('Discard unsaved changes?'),
        t('Discard')
      );
    } else {
      singleProcessingStore.deleteTranscriptDraft();
    }
  }

  saveDraft() {
    const draft = singleProcessingStore.getTranscriptDraft();
    if (
      draft?.languageCode !== undefined &&
      draft?.value !== undefined
    ) {
      singleProcessingStore.setTranscript(
        draft.languageCode,
        draft.value
      );
    }
  }

  openEditor() {
    const transcript = singleProcessingStore.getTranscript();
    if (transcript) {
      // Make new draft using existing transcript.
      singleProcessingStore.setTranscriptDraft(transcript);
    }
  }

  deleteTranscript() {
    destroyConfirm(
      singleProcessingStore.deleteTranscript.bind(singleProcessingStore),
      t('Delete transcript?')
    );
  }

  renderLanguageAndDate() {
    const storeTranscript = singleProcessingStore.getTranscript();
    const draft = singleProcessingStore.getTranscriptDraft();
    const valueLanguageCode = draft?.languageCode || storeTranscript?.languageCode;
    if (valueLanguageCode === undefined) {
      return null;
    }

    let dateText = '';
    if (storeTranscript) {
      if (storeTranscript.dateCreated !== storeTranscript?.dateModified) {
        dateText = t('last modified ##date##').replace('##date##', formatTime(storeTranscript.dateModified));
      } else {
        dateText = t('created ##date##').replace('##date##', formatTime(storeTranscript.dateCreated));
      }
    }

    return (
      <React.Fragment>
        <div>
          {t('Language')}
          <bem.ProcessingBody__transxHeaderLanguage>
            {envStore.getLanguageDisplayLabel(valueLanguageCode)}
          </bem.ProcessingBody__transxHeaderLanguage>
        </div>

        {dateText !== '' &&
          <bem.ProcessingBody__transxHeaderDate>
            {dateText}
          </bem.ProcessingBody__transxHeaderDate>
        }
      </React.Fragment>
    );
  }

  renderStepBegin() {
    const typeLabel = this.props.questionType || t('source file');
    return (
      <bem.ProcessingBody m='begin'>
        <p>{t('This ##type## does not have a transcript yet').replace('##type##', typeLabel)}</p>

        <Button
          type='full'
          color='blue'
          size='l'
          label={t('begin')}
          onClick={this.begin.bind(this)}
        />
      </bem.ProcessingBody>
    );
  }

  renderStepConfig() {
    const draft = singleProcessingStore.getTranscriptDraft();

    const typeLabel = this.props.questionType || t('source file');
    const languageSelectorTitle = t('Please selet the original language of the ##type##').replace('##type##', typeLabel);

    return (
      <bem.ProcessingBody m='config'>
        <LanguageSelector
          titleOverride={languageSelectorTitle}
          onLanguageChange={this.onLanguageChange.bind(this)}
          suggestedLanguages={singleProcessingStore.getAssetTranscriptableLanguages()}
        />

        <bem.ProcessingBody__footer>
          <Button
            type='bare'
            color='blue'
            size='m'
            label={t('back')}
            startIcon='caret-left'
            onClick={this.back.bind(this)}
          />

          <bem.ProcessingBody__footerRightButtons>
            <Button
              type='frame'
              color='blue'
              size='m'
              label={t('manual')}
              onClick={this.selectModeManual.bind(this)}
              isDisabled={draft?.languageCode === undefined}
            />

            <Button
              type='full'
              color='blue'
              size='m'
              label={t('automatic')}
              onClick={this.selectModeAuto.bind(this)}
              /*
              POST:
              {
                "googletx": {
                  "status": "requested",
                  "languageCode": "zh",
                }
              }
              will initiate the automatic translation
              */
            />
          </bem.ProcessingBody__footerRightButtons>
        </bem.ProcessingBody__footer>
      </bem.ProcessingBody>
    );
  }

  renderStepEditor() {
    const draft = singleProcessingStore.getTranscriptDraft();

    // The discard button will become a back button when there are no unsaved changes.
    let discardLabel = t('Back');
    if (singleProcessingStore.hasUnsavedTranscriptDraftValue()) {
      discardLabel = t('Discard');
    }

    return (
      <bem.ProcessingBody>
        <bem.ProcessingBody__transxHeader>
          {this.renderLanguageAndDate()}

          <bem.ProcessingBody__transxHeaderButtons>
            <Button
              type='frame'
              color='blue'
              size='s'
              label={discardLabel}
              onClick={this.discardDraft.bind(this)}
              isDisabled={singleProcessingStore.isFetchingData}
            />

            <Button
              type='full'
              color='blue'
              size='s'
              label={t('Save')}
              onClick={this.saveDraft.bind(this)}
              isPending={singleProcessingStore.isFetchingData}
              isDisabled={!singleProcessingStore.hasUnsavedTranscriptDraftValue()}
            />
          </bem.ProcessingBody__transxHeaderButtons>
        </bem.ProcessingBody__transxHeader>

        <bem.ProcessingBody__textarea
          value={draft?.value}
          onChange={this.onDraftValueChange.bind(this)}
          disabled={singleProcessingStore.isFetchingData}
        />
      </bem.ProcessingBody>
    );
  }

  renderStepViewer() {
    return (
      <bem.ProcessingBody>
        <bem.ProcessingBody__transxHeader>
          {this.renderLanguageAndDate()}

          <bem.ProcessingBody__transxHeaderButtons>
            <Button
              type='bare'
              color='storm'
              size='s'
              startIcon='edit'
              onClick={this.openEditor.bind(this)}
              tooltip={t('Edit')}
              isDisabled={singleProcessingStore.isFetchingData}
            />

            <Button
              type='bare'
              color='storm'
              size='s'
              startIcon='trash'
              onClick={this.deleteTranscript.bind(this)}
              tooltip={t('Delete')}
              isPending={singleProcessingStore.isFetchingData}
            />
          </bem.ProcessingBody__transxHeaderButtons>
        </bem.ProcessingBody__transxHeader>

        <bem.ProcessingBody__text>
          {singleProcessingStore.getTranscript()?.value}
        </bem.ProcessingBody__text>
      </bem.ProcessingBody>
    );
  }

  /** Identifies what step should be displayed based on the data itself. */
  render() {
    const draft = singleProcessingStore.getTranscriptDraft();

    // Step 1: Begin - the step where there is nothing yet.
    if (
      singleProcessingStore.getTranscript() === undefined &&
      draft === undefined
    ) {
      return this.renderStepBegin();
    }

    // Step 2: Config - for selecting the transcript language and mode.
    if (
      draft !== undefined &&
      (
        draft.languageCode === undefined ||
        draft.value === undefined
      )
    ) {
      return this.renderStepConfig();
    }

    // Step 3: Editor - display editor of draft transcript.
    if (draft !== undefined) {
      return this.renderStepEditor();
    }

    // Step 4: Viewer - display existing (on backend) transcript.
    if (
      singleProcessingStore.getTranscript() !== undefined &&
      draft === undefined
    ) {
      return this.renderStepViewer();
    }

    // Should not happen, but we need to return something.
    return null;
  }
}
